FROM mcr.microsoft.com/dotnet/runtime:8.0-bookworm-slim AS runtime
ARG TML_VERSION=v2026.08.3.0
ARG TML_SHA256=61e865f3702b12ce4a26c5a90b9de99a12c65ffc228455eb3390ce54af1eab15
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip python3 lib32gcc-s1 lib32stdc++6 libicu72 ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 terraria && mkdir -p /opt/tml /opt/mods /opt/steam /data /bridge \
    && chown -R 1000:1000 /opt/tml /opt/mods /opt/steam /data /bridge
RUN curl -fsSL "https://github.com/tModLoader/tModLoader/releases/download/${TML_VERSION}/tModLoader.zip" -o /tmp/tml.zip \
    && echo "${TML_SHA256}  /tmp/tml.zip" | sha256sum -c - \
    && unzip -q /tmp/tml.zip -d /opt/tml && rm /tmp/tml.zip
WORKDIR /opt/tml
USER 1000:1000
FROM runtime AS workshop
COPY --chown=1000:1000 mods.json install-mods.py /opt/
RUN curl -fsSL https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz | tar xz -C /opt/steam \
    && python3 /opt/install-mods.py

FROM runtime AS mod-build
COPY --chown=1000:1000 mod/ClaudeCompanion /tmp/ClaudeCompanion
RUN dotnet tModLoader.dll -server -nosteam -tmlsavedirectory /tmp/build-state -build /tmp/ClaudeCompanion \
    && cp /tmp/build-state/Mods/ClaudeCompanion.tmod /opt/mods/

FROM runtime AS server
COPY --from=workshop --chown=1000:1000 /opt/mods/ /opt/mods/
COPY --from=mod-build --chown=1000:1000 /opt/mods/ClaudeCompanion.tmod /opt/mods/
COPY --chown=1000:1000 server.py /opt/server.py
ENV HOME=/data DOTNET_CLI_TELEMETRY_OPTOUT=1
EXPOSE 7777/tcp
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/opt/server.py"]

FROM node:22-bookworm-slim AS bridge
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG CLAUDE_VERSION=2.1.306
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_VERSION} && npm cache clean --force
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir mcp==1.26.0
COPY bridge/ /app/
WORKDIR /app
USER 1000:1000
ENV HOME=/state PATH="/opt/venv/bin:${PATH}" PYTHONUNBUFFERED=1
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/app/bridge.py"]
