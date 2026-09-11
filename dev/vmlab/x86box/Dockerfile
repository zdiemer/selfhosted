# 86Box — the third engine, and the one that emulates PCs rather than runs them.
#
# QEMU is a virtualiser: it hands the guest a modern-ish machine and, with KVM,
# the host's own CPU. That is exactly right for Bazzite and exactly wrong for a
# 1997 operating system, which expects a specific chipset, a Sound Blaster at
# 220h, a period video card and a CPU that is not thousands of times too fast.
# Windows 98 under QEMU is a coin flip; under 86Box it is a Pentium II on an
# ASUS P2B-LS, which is a machine it shipped supporting.
#
# The wrapper is the same one the RPCEmu engine uses — Xvfb, x11vnc, noVNC on
# :8006 — for the same reason: 86Box is a Qt desktop application, and vmlab's
# proxy already knows how to reach a console on that port. 86Box does have a
# VNC renderer of its own, and it is NOT used: it is off by default upstream,
# has been reported uncompileable, and its own author notes it is barely tested.
# The route that already works here is worth more than one fewer process.

# --------------------------------------------------------------------------
# Build 86Box.
# --------------------------------------------------------------------------
FROM debian:bookworm-slim AS build

# Two things the upstream guide's Debian list does not cover, each of which
# stops cmake dead: libqt5opengl5-dev, because the Qt frontend does
# find_package(Qt5 ... OpenGL); and libsdl2-dev, because it also does
# find_package(SDL2) further down qt/CMakeLists.txt than anyone reads.
# The guide names libsdl3-dev, which bookworm does not have - but SDL3 is
# only reached through FluidSynth, and that is off below. (not in
# bookworm) — FluidSynth and the SDL frontend. The Qt frontend needs no SDL at
# all, which is why this builds on a stable base rather than chasing testing.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake extra-cmake-modules pkg-config ninja-build git \
      ca-certificates \
      libfreetype-dev libzstd-dev libpng-dev libopenal-dev librtmidi-dev \
      libsdl2-dev \
      libsndfile1-dev libserialport-dev libslirp-dev \
      libevdev-dev libxkbcommon-dev libxkbcommon-x11-dev \
      qtbase5-dev qtbase5-private-dev qttools5-dev libqt5opengl5-dev \
    && rm -rf /var/lib/apt/lists/*

# Pinned to the commit the tag pointed at, not to a tarball checksum: GitHub's
# generated archives are not guaranteed byte-stable, and git is content-addressed
# already, so this is the stronger pin and a cheaper one to verify.
ARG BOX_TAG=v6.0
ARG BOX_COMMIT=4fef696a4eead1d55a28d6ac0e5bd2864e5454da
RUN set -eux; \
    git clone --depth 1 --branch "${BOX_TAG}" https://github.com/86Box/86Box /src; \
    test "$(git -C /src rev-parse HEAD)" = "${BOX_COMMIT}"

# DYNAREC because a period CPU still has to be emulated on a modern one and the
# recompiler is the difference between "slow" and "unusable". The rest are off
# to keep the dependency surface down: there is no MIDI device, no Discord and
# no soundfont in a pod, and every one of them is another library to carry.
RUN set -eux; \
    cmake -S /src -B /build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DRELEASE=ON -DDYNAREC=ON -DQT=ON \
      -DFLUIDSYNTH=OFF -DMUNT=OFF -DSOUNDCANVAS=OFF -DDISCORD=OFF -DRTMIDI=OFF; \
    cmake --build /build -j"$(nproc)"; \
    find /build -name '86Box' -type f -perm -u+x -exec cp {} /usr/local/bin/86Box \; ; \
    test -x /usr/local/bin/86Box

# --------------------------------------------------------------------------
# The ROMs.
#
# 86Box emulates real machines, and a real machine will not POST without its
# BIOS. These are vendor ROMs, and 86Box distributes the set itself as the
# supported way to run it — this pulls from that repository, at the commit its
# v6.0 tag points at, matching the emulator built above. ROM and emulator
# version are kept in step deliberately: 86Box adds and renames machines
# between releases, and a machine whose ROM directory has moved simply fails to
# start with a message about a missing ROM.
# --------------------------------------------------------------------------
FROM debian:bookworm-slim AS roms

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates && rm -rf /var/lib/apt/lists/*

ARG ROMS_TAG=v6.0
ARG ROMS_COMMIT=26761b5ab25786b4405c6829236fa745436e5399
RUN set -eux; \
    git clone --depth 1 --branch "${ROMS_TAG}" https://github.com/86Box/roms /roms; \
    test "$(git -C /roms rev-parse HEAD)" = "${ROMS_COMMIT}"; \
    rm -rf /roms/.git; \
    chmod -R a+rX /roms; \
    test -d /roms/machines

# --------------------------------------------------------------------------
# Runtime.
# --------------------------------------------------------------------------
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      libqt5widgets5 libqt5gui5 libqt5core5a libqt5network5 libqt5opengl5 \
      libopenal1 libsndfile1 libserialport0 libslirp0 libpng16-16 \
      libfreetype6 libevdev2 libxkbcommon0 libxkbcommon-x11-0 libzstd1 \
      libsdl2-2.0-0 \
      xvfb x11-utils x11vnc novnc websockify python3-minimal \
      fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/bin/86Box /usr/local/bin/86Box
# Read-only, in the image, and NOT copied into the session's volume: the set is
# ~95MB and identical for every guest, so `--rompath` points at it instead.
COPY --from=roms  /roms /usr/share/86box/roms

RUN printf '%s' \
    '<!doctype html><meta charset=utf-8><title>86Box</title>' \
    '<script>location.replace("vnc.html?autoconnect=true&resize=scale&reconnect=true")</script>' \
    > /usr/share/novnc/index.html

# Xvfb wants this to exist and be root-owned before it will bind its socket, and
# the session runs as uid 10001. Same reason as the RPCEmu image.
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

COPY first-boot-keys.py /usr/local/bin/first-boot-keys.py
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

RUN useradd -u 10001 -m -s /usr/sbin/nologin box86
USER 10001
ENV HOME=/tmp \
    XDG_RUNTIME_DIR=/tmp/xdg \
    QT_QPA_PLATFORM=xcb \
    DISPLAY=:0
EXPOSE 8006 5900
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
