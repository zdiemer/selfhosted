# Builds contain code derived from the owner's retail binaries: private registry only.
FROM ubuntu:24.04 AS build
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git python3 python3-venv build-essential cmake ninja-build \
    pkg-config wget xz-utils fonts-liberation && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
WORKDIR /src
ARG SOURCE_COMMIT
RUN git init . && git remote add origin https://github.com/zdiemer/redalert2-recomp.git && \
    git fetch --depth 1 origin "$SOURCE_COMMIT" && git checkout --detach FETCH_HEAD
# Binary inputs use a temporary named context outside the repository.
COPY --from=retail /game.exe /src/game/game.exe
RUN python tools/wasm/bootstrap.py --target game --game-dir /src/game
RUN python tools/wasm/build.py --target game --jobs 3
COPY --from=retail /gamemd.exe /src/game/gamemd.exe
RUN python tools/wasm/bootstrap.py --target gamemd --game-dir /src/game
RUN python tools/wasm/build.py --target gamemd --jobs 3
RUN mkdir -p /output/game /output/gamemd && \
    cp build-wasm-game/ra2.* /output/game/ && cp build-wasm/ra2.* /output/gamemd/ && \
    cp /usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf /output/arial.ttf && \
    cp LICENSE /output/LICENSE && \
    cp .deps/ffmpeg/COPYING.LGPLv2.1 /output/FFmpeg-LICENSE && \
    test -s /output/game/ra2.wasm && test -s /output/gamemd/ra2.wasm
# LGPL corresponding decoder source; generated retail code stays private.
RUN git -C .deps/ffmpeg archive --format=tar.gz HEAD > /output/ffmpeg-source.tar.gz

FROM nginxinc/nginx-unprivileged:1.31-alpine
COPY --from=build /output/ /usr/share/nginx/html/
COPY nginx.conf /etc/nginx/conf.d/default.conf
