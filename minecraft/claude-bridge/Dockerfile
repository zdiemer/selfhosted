# Base on node — Claude Code CLI ships as an npm package and pulls its own
# self-contained runtime, so node is the most direct path. Bookworm-slim
# keeps the image under 200MB without the ergonomic loss of alpine
# (claude-code's launcher script uses bash + glibc).
FROM node:22-bookworm-slim

# Python for the bridge + MCP, git for FEEDBACK.md commits, ca-certificates
# for HTTPS to api.anthropic.com / github.com / k8s API. tini gives us a
# real PID 1 so SIGTERM during pod termination cleanly stops Claude subprocs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        git ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Claude Code CLI. Pinned to a known version so image rebuilds are
# reproducible — bump deliberately when we want a newer CLI.
RUN npm install -g @anthropic-ai/claude-code@latest \
 && npm cache clean --force

# Non-root user. UID 1000 matches the node image's default `node` user; we
# reuse it so npm-installed binaries are already on PATH for it.
RUN install -d -o node -g node /app /app/state \
 && install -d -o node -g node /home/node/.claude
USER node
WORKDIR /app

# Python deps go in a venv owned by `node` so we don't need root at runtime.
COPY --chown=node:node src/requirements.txt /app/requirements.txt
RUN python3 -m venv /app/venv \
 && /app/venv/bin/pip install --no-cache-dir -r /app/requirements.txt

COPY --chown=node:node src/ /app/src/
COPY --chown=node:node claude-config/ /app/claude-config/

ENV PATH="/app/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    # Claude Code reads ~/.claude — the Deployment sets HOME=/app/state and
    # the init container creates /app/state/.claude/.credentials.json as a
    # symlink to the host's credentials file.
    HOME=/app/state

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/app/src/bridge.py"]
