# syntax=docker/dockerfile:1.7
# Garlemald Server — a FINAL FANTASY XIV 1.23b (the original 2010 game)
# server emulator: the Rust port of Project Meteor, four binaries
# (web / lobby / world / map) sharing one SQLite file and driving Meteor's
# 1,142 Lua content scripts. Built from the upstream git repo at the commit in
# values.yaml (image.upstreamRef) — build.sh passes the repo URL#ref as the
# build context, so there is no source in this directory.
#
# rust:1.95 because rust-toolchain.toml pins 1.95.0 (edition 2024). SQLite and
# Lua are vendored by their crates; only a C toolchain is needed, which the
# rust image has.

FROM rust:1.95-bookworm AS build
WORKDIR /src
COPY . .
# --locked: the lockfile is the build's pin; a resolver drift is an error.
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    cargo build --release --workspace --locked \
 && mkdir -p /out/bin \
 && cp target/release/web-server target/release/lobby-server \
       target/release/world-server target/release/map-server /out/bin/

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 garlemald && useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin garlemald \
    && mkdir -p /srv/garlemald/data /srv/garlemald/logs /srv/garlemald/configs \
    && chown -R garlemald:garlemald /srv/garlemald
COPY --from=build /out/bin/ /usr/local/bin/
# The content: Lua scripts the map-server loads at boot. Read-only, in the image.
COPY --from=build /src/scripts/lua /srv/garlemald/scripts/lua
WORKDIR /srv/garlemald
USER garlemald
ENTRYPOINT ["/usr/bin/tini", "--"]
