# Workspace image: Claude Code CLI + happy CLI (mobile remote control, see
# dev/happy-server) + ttyd web terminal + tmux, on the same node base as
# minecraft/claude-bridge. One image serves every pod container (term,
# tailscaled, init) — they differ only in `command:`.
FROM node:22-bookworm-slim

# ttyd isn't packaged in bookworm; ship the static release binary. Pinned so
# rebuilds are reproducible — bump deliberately.
ARG TTYD_VERSION=1.7.7
# Cluster tooling, all pinned static binaries:
# kubectl tracks the cluster minor (v1.34.x) — bump after scripts/k3s/k3s-upgrade.sh.
ARG KUBECTL_VERSION=v1.34.6
ARG HELM_VERSION=v3.19.0
# tailscaled runs in-pod with userspace networking (no TUN, no root) so the
# scripts/k3s maintenance scripts can `tailscale ssh root@<node>`.
ARG TAILSCALE_VERSION=1.88.1
# buildctl client only — builds run on the in-cluster buildkitd from
# infra/buildkit. MUST stay in lockstep with that chart's image tag.
ARG BUILDKIT_VERSION=v0.24.0
# sops for repos that keep secrets encrypted in-git (talaria's
# secrets.prod.yaml); age support is built in, no age binary needed.
# Key expected at ~/.age/keys.txt via SOPS_AGE_KEY_FILE (see README).
ARG SOPS_VERSION=v3.10.2
# GitHub CLI — create repos, open PRs, and push from the workspace (Claude
# scaffolds new app repos here). Auth is a one-time `gh auth login` persisted on
# the home PVC under ~/.config/gh (see README "Cluster powers"), same shape as
# the tailscale/docker/happy logins — no token is baked into the image.
ARG GH_VERSION=2.96.0
# Bakery (github.com/seemethere/bakery, npm name pi-web-agent) — a web coding
# harness served under /bakery's own subdomain (see README "Bakery surface").
# It's a Bun monorepo (server + Vite web); there's no published image, so we
# vendor the source at a pinned ref and `bun install` at build time. Bun is
# minor-pinned (upstream ships on `oven/bun:1`); BAKERY_REF is pinned to the
# commit the v3 image was built from (upstream cuts no release tags) — bump
# deliberately, same as the claude CLI.
ARG BUN_VERSION=1.2
ARG BAKERY_REF=a666a6d2302a7db1d6649eae519dd44d407c069c
# 1Password CLI. The pod used to have no path to the vault at all — secrets
# arrived only in the cluster relay bundle, so an edit made HERE was stranded
# until the laptop published again. With op present, scripts/op-session.sh signs
# in unattended from ~/.config/selfhosted/op-password and `secrets.sh sync`
# works the same here as on the laptop. The relay stays as the bootstrap path.
ARG OP_VERSION=2.38.1
# signal-cli (GraalVM native build, no JRE) powers the messaging surface
# (gateway/ + the signal-cli pod container). Signal's servers reject stale
# clients, so bump this deliberately but don't let it rot — a too-old client
# stops receiving. Registration state lives on the PVC (~/.local/share/signal-cli).
ARG SIGNAL_CLI_VERSION=0.14.7

# git/openssh for repo work, tmux for session persistence, ripgrep/jq/less/
# procps/curl as the dev baseline claude expects, python3 for scripting, tini
# as PID 1 so SIGTERM cleanly reaps claude subprocesses.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git openssh-client tmux curl ca-certificates \
        ripgrep jq less procps python3 python3-yaml tini unzip \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /usr/local/bin/ttyd \
        "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64" \
 && chmod 0755 /usr/local/bin/ttyd

# Cluster tooling as static binaries (no apt sources, no systemd units): the pod
# runs as uid 1000 with a read-only rootfs, so everything must work from
# /usr/local/bin with state under $HOME or /tmp.
RUN curl -fsSL -o /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
 && chmod 0755 /usr/local/bin/kubectl \
 && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" \
    | tar xz -C /usr/local/bin --strip-components=1 linux-amd64/helm \
 && curl -fsSL "https://github.com/moby/buildkit/releases/download/${BUILDKIT_VERSION}/buildkit-${BUILDKIT_VERSION}.linux-amd64.tar.gz" \
    | tar xz -C /usr/local/bin --strip-components=1 bin/buildctl \
 && curl -fsSL -o /usr/local/bin/sops \
        "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.amd64" \
 && chmod 0755 /usr/local/bin/sops \
 && curl -fsSL -o /tmp/op.zip \
        "https://cache.agilebits.com/dist/1P/op2/pkg/v${OP_VERSION}/op_linux_amd64_v${OP_VERSION}.zip" \
 && unzip -p /tmp/op.zip op > /usr/local/bin/op \
 && chmod 0755 /usr/local/bin/op \
 && rm -f /tmp/op.zip \
 && curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
    | tar xz -C /usr/local/bin --strip-components=2 "gh_${GH_VERSION}_linux_amd64/bin/gh" \
 && curl -fsSL "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}-Linux-native.tar.gz" \
    | tar xz -C /usr/local/bin signal-cli \
 && chmod 0755 /usr/local/bin/signal-cli \
 && curl -fsSL "https://pkgs.tailscale.com/stable/tailscale_${TAILSCALE_VERSION}_amd64.tgz" \
    | tar xz -C /usr/local/bin --strip-components=1 \
        "tailscale_${TAILSCALE_VERSION}_amd64/tailscale" \
        "tailscale_${TAILSCALE_VERSION}_amd64/tailscaled" \
 && mv /usr/local/bin/tailscale /usr/local/bin/tailscale.real \
 && printf '#!/bin/sh\nexec /usr/local/bin/tailscale.real --socket=/tmp/tailscale/tailscaled.sock "$@"\n' \
        > /usr/local/bin/tailscale \
 && chmod 0755 /usr/local/bin/tailscale
# The `tailscale` wrapper exists because the CLI has no socket env var, only
# --socket, and the default /var/run/tailscale is unwritable here (read-only
# rootfs, uid 1000). Pointing it at the tailscaled container's socket on the
# shared /tmp emptyDir keeps scripts/k3s/_common.sh's `tailscale ssh` working
# unmodified.

# claude-code tracks latest (bump = rebuild, same as claude-bridge); happy is
# pinned — it must track the self-hosted relay (dev/happy-server, HAPPY_REF in
# its build.sh), so upgrade the pair deliberately. Run `happy` instead of
# `claude` in tmux to get phone remote control; plain `claude` still works.
# Bun is the runtime bakery needs (its server + Vite web both run under `bun`).
RUN npm install -g @anthropic-ai/claude-code@latest happy@1.2.0 "bun@${BUN_VERSION}" \
 && npm cache clean --force

# Vendor bakery at a pinned ref and install its deps at build time. The pod's
# rootfs is read-only for the other surfaces, but the bakery containers run
# with readOnlyRootFilesystem:false (bun/vite write transpile caches into the
# tree), so /opt/bakery must be owned by uid 1000. bun install runs the repo's
# postinstall hook; egress 80/443 is open on buildkitd (infra/buildkit).
RUN git clone --filter=blob:none https://github.com/seemethere/bakery /opt/bakery \
 && git -C /opt/bakery checkout "${BAKERY_REF}" \
 && ( cd /opt/bakery && HOME=/root bun install ) \
 && chown -R 1000:1000 /opt/bakery \
 && rm -rf /root/.bun/install/cache

# The messaging gateway (Signal/WhatsApp → headless claude, see gateway/ and
# README "Messaging surface"). In-repo app baked into this image like bakery;
# deps resolve at build time so the pod never runs a package install. Owned by
# uid 1000 for symmetry with /opt/bakery, though it runs read-only.
COPY --chown=1000:1000 gateway /opt/messaging-gateway
RUN cd /opt/messaging-gateway \
 && HOME=/root bun install --frozen-lockfile --production \
 && chown -R 1000:1000 /opt/messaging-gateway/node_modules \
 && rm -rf /root/.bun/install/cache

# UTF-8 so the claude TUI's box-drawing renders in tmux/ttyd.
# BUILDKIT_HOST points buildctl at the in-cluster buildkitd (infra/buildkit) so
# the per-chart build.sh scripts work from inside the pod with no flags.
ENV HOME=/home/node \
    LANG=C.UTF-8 \
    BUILDKIT_HOST=tcp://buildkitd.buildkit.svc.cluster.local:1234

# UID 1000 = the base image's `node` user; matches the pod securityContext.
# /home/node itself is replaced by the PVC mount at runtime.
USER node
WORKDIR /home/node

ENTRYPOINT ["/usr/bin/tini", "--"]
# No CMD — each pod container sets its own `command:`.
