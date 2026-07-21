# Workspace image: Claude Code CLI + CloudCLI (claudecodeui) web UI + ttyd web
# terminal + tmux, on the same node base as minecraft/claude-bridge. One image
# serves both pod containers — they differ only in `command:` (cloudcli vs
# ttyd), which keeps the two surfaces version-locked to the same claude CLI.
FROM node:22-bookworm-slim

# ttyd isn't packaged in bookworm; ship the static release binary. Pinned so
# rebuilds are reproducible — bump deliberately.
ARG TTYD_VERSION=1.7.7

# git/openssh for repo work, tmux for session persistence, ripgrep/jq/less/
# procps/curl as the dev baseline claude expects, python3 for scripting, tini
# as PID 1 so SIGTERM cleanly reaps claude subprocesses.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git openssh-client tmux curl ca-certificates \
        ripgrep jq less procps python3 tini \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /usr/local/bin/ttyd \
        "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64" \
 && chmod 0755 /usr/local/bin/ttyd

# claude-code tracks latest (bump = rebuild, same as claude-bridge); CloudCLI
# is pinned — it's the web-facing surface, upgrade it deliberately.
RUN npm install -g @anthropic-ai/claude-code@latest @cloudcli-ai/cloudcli@1.36.3 \
 && npm cache clean --force

# UTF-8 so the claude TUI's box-drawing renders in tmux/ttyd.
ENV HOME=/home/node \
    LANG=C.UTF-8

# UID 1000 = the base image's `node` user; matches the pod securityContext.
# /home/node itself is replaced by the PVC mount at runtime.
USER node
WORKDIR /home/node

ENTRYPOINT ["/usr/bin/tini", "--"]
# No CMD — each pod container sets its own `command:`.
