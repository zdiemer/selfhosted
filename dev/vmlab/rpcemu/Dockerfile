# RPCEmu — the second engine in this lab, and the only way RISC OS runs here.
#
# Everything else in vmlab is qemux/qemu: one image, one ISO, a machine
# assembled from validated flags. RISC OS does not fit that shape, and
# values.yaml records the whole investigation into why. The short version is
# that no QEMU machine type boots it — QEMU's raspi machines never run the
# VideoCore bootloader, and RISC OS's own developers have failed to get past a
# black screen with -kernel since 2016.
#
# RPCEmu emulates the Acorn Risc PC, which is hardware RISC OS actually has a
# HAL for, so it simply works. The cost is that it is a Qt desktop application
# rather than a headless VMM: it needs an X server, and something to turn that
# X server into the noVNC-on-8006 that the vmlab proxy already knows how to
# reach. Hence Xvfb + x11vnc + websockify below.
#
# Unlike a qemux guest, this image carries its own OS. That is not laziness
# about the ISO cache — it is that RPCEmu is not a general-purpose machine with
# interchangeable media. It emulates exactly one computer, and RISC OS 5 needs
# exactly one matching pair: the IOMD softload ROM and the HardDisc4 boot disc,
# both published by RISC OS Open under the Apache 2.0 licence. Splitting a
# fixed pair across a one-URL catalog field would be pretending they are
# swappable. The engine IS RISC OS, and it needs no network to start.

# --------------------------------------------------------------------------
# Build RPCEmu itself.
# --------------------------------------------------------------------------
FROM debian:bookworm-slim AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl \
      qtbase5-dev qtmultimedia5-dev qtchooser qt5-qmake \
    && rm -rf /var/lib/apt/lists/*

# Upstream (Spoon Edition) rather than a fork. Pinned by hash because this is
# the one download here that is not signed and not versioned by content.
ARG RPCEMU_VERSION=0.9.5
ARG RPCEMU_SHA256=4e4624641cd1a83af275bd6da8765d4a0e5e0c83f008f059014cc24c5c33d59e
RUN set -eux; \
    curl -fsSL -o /tmp/rpcemu.tar.gz \
      "https://www.marutan.net/rpcemu/${RPCEMU_VERSION}/rpcemu-${RPCEMU_VERSION}.tar.gz"; \
    echo "${RPCEMU_SHA256}  /tmp/rpcemu.tar.gz" | sha256sum -c -; \
    mkdir -p /src; \
    tar xzf /tmp/rpcemu.tar.gz -C /src --strip-components=1

# `dynarec` builds the recompiler rather than the interpreter. rpcemu.pro has an
# amd64 code generator (codegen_amd64.c) and these nodes are amd64, so this is a
# real JIT and not a fallback — which matters, because unlike every other guest
# in the lab this one gets no KVM and never could: it is a different processor
# architecture, translated.
WORKDIR /src/src/qt5
RUN set -eux; \
    sed -i 's/^CONFIG += debug_and_release$/CONFIG += release dynarec/' rpcemu.pro; \
    grep -q 'CONFIG += release dynarec' rpcemu.pro; \
    qmake -qt=5 && make -j"$(nproc)"; \
    test -x /src/rpcemu-recompiler

# --------------------------------------------------------------------------
# Fetch RISC OS. Kept in its own stage so the ~45MB of zips never reach the
# final image, only what comes out of them.
# --------------------------------------------------------------------------
FROM debian:bookworm-slim AS riscos

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip python3 && rm -rf /var/lib/apt/lists/*
# python3, not python3-minimal: the extractor needs zipfile, which pulls in
# shutil, which python3-minimal does not ship.

COPY unzip-riscos.py /usr/local/bin/unzip-riscos.py

# The IOMD softload ROM: RISC OS 5.30 for the Risc PC / A7000, which is the
# machine RPCEmu emulates. roms/roms.txt in the tarball explains the rule this
# satisfies — every non-".txt" file in roms/ is concatenated in alphabetical
# order and the total must be 2, 4, 6 or 8MiB. One 4MiB file, so: one ROM.
ARG IOMD_SHA256=69defdaa0429c0cb22432eb5df648cde51a73b1810cf77e6ccaded2418853dfa
ARG HD4_SHA256=2d0ae90df9412622950b05d1b95dbb07ed95c144213e7d677401a75c330c570e
RUN set -eux; \
    curl -fsSL -o /tmp/iomd.zip \
      "https://www.riscosopen.org/zipfiles/platform/riscpc/IOMD-Soft.5.30.zip"; \
    echo "${IOMD_SHA256}  /tmp/iomd.zip" | sha256sum -c -; \
    mkdir -p /riscos/roms; \
    unzip -p /tmp/iomd.zip 'soft/!Boot/Resources/SoftLoad/riscos' > /riscos/roms/riscos; \
    test "$(stat -c%s /riscos/roms/riscos)" = 4194304

# Extracted with the script rather than unzip, and that is load-bearing: RISC OS
# keeps a file's TYPE beside it rather than in its name, ROOL's zip carries the
# type in an Acorn extra field, and unzip throws that away. A plainly-unzipped
# HardDisc4 boots as far as !Boot and then fails every RMEnsure in it, because
# every module in the boot sequence is now a text file. Watched it happen.
#
# HardDisc4 is the boot disc — !Boot, the fonts, the apps, everything that makes
# the ROM into a desktop. RPCEmu surfaces the hostfs directory to RISC OS as
# HostFS::HardDisc4, so the zip's HardDisc4/ contents land at its root.
RUN set -eux; \
    curl -fsSL -o /tmp/hd4.zip \
      "https://www.riscosopen.org/zipfiles/platform/common/HardDisc4.5.30.zip"; \
    echo "${HD4_SHA256}  /tmp/hd4.zip" | sha256sum -c -; \
    mkdir -p /riscos/hostfs; \
    python3 /usr/local/bin/unzip-riscos.py /tmp/hd4.zip HardDisc4/ /riscos/hostfs; \
    test -d '/riscos/hostfs/!Boot'; \
    # An application directory keeps its name; it is the files inside that
    # carry the type. This asserts one of those actually arrived typed.
    test -e '/riscos/hostfs/!Boot/!Run,feb'

# The zip carries RISC OS's own permissions, which unzip renders as owner-only.
# The session runs as uid 10001, so without this the template is unreadable and
# the copy below fails on every single directory. RISC OS filetypes live in the
# ",xxx" filename suffix rather than in a mode bit, so nothing is lost by
# flattening these.
RUN chmod -R a+rX /riscos

# --------------------------------------------------------------------------
# Runtime.
# --------------------------------------------------------------------------
FROM debian:bookworm-slim

# libqt5multimedia5 is needed even with sound off: rpcemu links it
# unconditionally. x11vnc gives the RFB port that vmlab's screenshot and
# live-tile code already speaks, and websockify serves noVNC over the same 8006
# the qemux image uses, so proxy.py needs no idea which engine it is talking to.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libqt5widgets5 libqt5gui5 libqt5core5a libqt5multimedia5 \
      libqt5multimedia5-plugins \
      xvfb x11vnc novnc websockify python3-minimal \
      x11-utils xkb-data \
      procps \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/rpcemu-recompiler /usr/local/bin/rpcemu
# The template tree. Copied to a writable directory at startup, so a stopped
# session leaves nothing behind and a persistent one has somewhere to write.
COPY --from=build /src/poduleroms /template/poduleroms
COPY --from=build /src/netroms    /template/netroms
# The CMOS is not incidental. RISC OS reads its boot filesystem out of it, and a
# machine with no CMOS comes up at the supervisor `*` prompt instead of the
# desktop — which is exactly what the first working boot here did. Upstream
# ships one already configured to boot from HostFS, so it is carried across
# rather than reconstructed with *Configure incantations.
COPY --from=build /src/cmos.ram   /template/cmos.ram
COPY --from=riscos /riscos/roms    /template/roms
COPY --from=riscos /riscos/hostfs  /template/hostfs

# noVNC's own page, not a hand-rolled one. /vm/<sid>/ arrives here as "/" after
# the proxy strips the prefix, and noVNC resolves both its assets and its
# websocket relative to location.href — the same reason the qemux consoles work
# through that proxy unmodified.
RUN printf '%s' \
    '<!doctype html><meta charset=utf-8><title>RISC OS</title>' \
    '<script>location.replace("vnc.html?autoconnect=true&resize=scale&reconnect=true")</script>' \
    > /usr/share/novnc/index.html

# Xvfb wants this to exist and to be owned by root before it will bind its
# socket, and the session runs as uid 10001 — so it is made here, not there.
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

COPY configure-cmos.py /usr/local/bin/configure-cmos.py
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Same posture as a session pod: no root, no capabilities, nothing to escalate.
# uid 10001 matches the rest of the lab.
RUN useradd -u 10001 -m -s /usr/sbin/nologin rpcemu
USER 10001
ENV HOME=/tmp \
    XDG_RUNTIME_DIR=/tmp/xdg \
    QT_QPA_PLATFORM=xcb \
    DISPLAY=:0
EXPOSE 8006 5900
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
