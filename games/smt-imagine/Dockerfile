# syntax=docker/dockerfile:1.7
# COMP_hack 4.12.2 "Wyrd" — the SMT: IMAGINE server emulator — built from the
# Launchpad PPA source tarball. That tarball is the only surviving COPY of the
# full tree: github.com/comphack is an empty org, the HyperChiicken/SMT fork has
# lost its libcomp and datastore submodules, and the PPA's .tar.xz vendors both
# (179 libcomp sources, 2,944 datastore files). Pinned by sha256; if Launchpad
# ever drops it, infra/registry has the built image and ~/smt-work has a copy.
#
# bullseye, deliberately: the project targets Ubuntu 16.04 / GCC 5, and
# bullseye is the newest Debian that still ships libqt5webkit5 (the build
# wants Qt5Widgets at the top level even though the servers never link it).
# Build flags mirror debian/rules in the tarball.

ARG SRC_URL=https://launchpad.net/~compomega/+archive/ubuntu/comphack/+sourcefiles/comphack/4.12.2/comphack_4.12.2.tar.xz
ARG SRC_SHA256=643ea6376d33c9b3481e545071f96e8c8afcb4a6fc8c0e9e0d62ca9473d46119

FROM debian:bullseye-slim AS build
ARG SRC_URL
ARG SRC_SHA256
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake git ca-certificates xz-utils unzip \
      qtbase5-dev libqt5webkit5-dev \
      libssl-dev libphysfs-dev zlib1g-dev default-libmysqlclient-dev libsystemd-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
ADD --checksum=sha256:${SRC_SHA256} ${SRC_URL} /tmp/comphack.tar.xz
RUN tar -xJf /tmp/comphack.tar.xz --strip-components=1 && rm /tmp/comphack.tar.xz
# libcomp::String::Arg matches its "%1" placeholders with the regex "\%([0-9]+)".
# The escaped percent was harmless on GCC 5's libstdc++ and never matches on
# GCC 10's, so every formatted message came out as "Argument not found in
# string: %1%2" — and, worse, every SQL statement and path built the same way
# came out blank. % is not a regex metacharacter; drop the escape.
RUN grep -q 'std::regex re("\\\\%(\[0-9\]+)")' libcomp/libcomp/src/CString.cpp \
 && sed -i 's/std::regex re("\\\\%(\[0-9\]+)")/std::regex re("%([0-9]+)")/' libcomp/libcomp/src/CString.cpp \
 && grep -q 'std::regex re("%(\[0-9\]+)")' libcomp/libcomp/src/CString.cpp

WORKDIR /src/build
RUN cmake -DNO_WARNINGS:BOOL=ON -DDISABLE_TESTING:BOOL=ON \
      -DCMAKE_BUILD_TYPE:STRING=RelWithDebInfo -DGENERATE_DOCUMENTATION=OFF \
      -DUSE_SYSTEM_ZLIB=ON -DUSE_SYSTEM_OPENSSL=ON -DUSE_SYSTEM_PHYSFS=ON \
      -DUSE_SYSTEM_MARIADB=ON -DCMAKE_INSTALL_PREFIX=/usr .. \
 && make -j"$(( $(nproc) > 12 ? 12 : $(nproc) ))"
RUN make install DESTDIR=/out && ls -la /out/usr/bin

FROM debian:bullseye-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      libssl1.1 libphysfs1 zlib1g libmariadb3 libsystemd0 tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 comphack && useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin comphack \
    && mkdir -p /etc/comp_hack /var/lib/comp_hack/database /var/lib/comp_hack/data /var/log/comp_hack \
    && chown -R comphack:comphack /var/lib/comp_hack /var/log/comp_hack
COPY --from=build /out/usr/bin/comp_lobby /out/usr/bin/comp_world /out/usr/bin/comp_channel \
                  /out/usr/bin/comp_encrypt /out/usr/bin/comp_decrypt /out/usr/bin/comp_rehash \
                  /out/usr/bin/comp_verify /usr/bin/
COPY --from=build /src/datastore /var/lib/comp_hack/datastore
COPY --from=build /src/contrib/webroot /var/lib/comp_hack/webroot
# Two config files the servers read from /etc/comp_hack that carry no site
# settings: the game constants table and the new-character template. The
# chart mounts its own lobby/world/channel/setup.xml beside them.
COPY --from=build /src/contrib/testing/config/constants.xml /src/contrib/winconfig/newcharacter.xml /etc/comp_hack/
USER comphack
ENTRYPOINT ["/usr/bin/tini", "--"]
