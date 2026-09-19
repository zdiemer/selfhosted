# Workspace image: Claude Code CLI + happy CLI (mobile remote control, see
# dev/happy-server) + ttyd web terminal + tmux, on the same node base as
# minecraft/claude-bridge. One image serves every pod container (term,
# tailscaled, init) — they differ only in `command:`.

# ACP adapter for Muse Code (Muse speaks Meta's own MSP over `muse serve`, not
# ACP). Built from source rather than taken from the release tarballs, which are
# all -unknown-linux-gnu built on a newer CI: the published binary dies here with
# "GLIBC_2.39 not found", because bookworm has 2.36. Cheap to build — the crate's
# [dependencies] is EMPTY ("the shipped binary remains dependency-free"), so this
# stage compiles std-only Rust and pulls nothing from crates.io.
# The pinned tag is the trust anchor, same as BAKERY_REF below.
# renovate: datasource=github-releases depName=BrokkAi/muse-acp extractVersion=^v(?<version>.+)$
ARG MUSE_ACP_VERSION=0.4.5
# renovate: datasource=docker depName=rust
ARG RUST_VERSION=1.98.1
FROM rust:${RUST_VERSION}-slim-bookworm AS muse-acp-build
ARG MUSE_ACP_VERSION
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && git clone --depth 1 --branch "v${MUSE_ACP_VERSION}" \
        https://github.com/BrokkAi/muse-acp /src \
 && cargo build --locked --release --manifest-path /src/Cargo.toml

FROM node:22-bookworm-slim

# ttyd isn't packaged in bookworm; ship the static release binary. Pinned so
# rebuilds are reproducible — bump deliberately (Renovate proposes, human merges).
# renovate: datasource=github-releases depName=tsl0922/ttyd
ARG TTYD_VERSION=1.7.7
# Cluster tooling, all pinned static binaries:
# kubectl tracks the cluster minor (v1.34.x) — bump after scripts/k3s/k3s-upgrade.sh.
# renovate: datasource=github-tags depName=kubernetes/kubernetes
ARG KUBECTL_VERSION=v1.37.0
# renovate: datasource=github-releases depName=helm/helm
ARG HELM_VERSION=v3.22.0
# tailscaled runs in-pod with userspace networking (no TUN, no root) so the
# scripts/k3s maintenance scripts can `tailscale ssh root@<node>`.
# renovate: datasource=github-releases depName=tailscale/tailscale extractVersion=^v(?<version>.+)$
ARG TAILSCALE_VERSION=1.102.4
# buildctl client only — builds run on the in-cluster buildkitd from
# infra/buildkit. MUST stay in lockstep with that chart's image tag.
# renovate: datasource=github-releases depName=moby/buildkit
ARG BUILDKIT_VERSION=v0.33.0
# sops for repos that keep secrets encrypted in-git (talaria's
# secrets.prod.yaml); age support is built in, no age binary needed.
# Key expected at ~/.age/keys.txt via SOPS_AGE_KEY_FILE (see README).
# renovate: datasource=github-releases depName=getsops/sops
ARG SOPS_VERSION=v3.13.3
# GitHub CLI — create repos, open PRs, and push from the workspace (Claude
# scaffolds new app repos here). Auth is a one-time `gh auth login` persisted on
# the home PVC under ~/.config/gh (see README "Cluster powers"), same shape as
# the tailscale/docker/happy logins — no token is baked into the image.
# renovate: datasource=github-releases depName=cli/cli extractVersion=^v(?<version>.+)$
ARG GH_VERSION=2.101.0
# Bakery (github.com/seemethere/bakery, npm name pi-web-agent) — a web coding
# harness served under /bakery's own subdomain (see README "Bakery surface").
# It's a Bun monorepo (server + Vite web); there's no published image, so we
# vendor the source at a pinned ref and `bun install` at build time. Bun is
# exact-pinned for reproducible builds (Renovate bumps within 1.x; majors are
# gated); BAKERY_REF is pinned to the commit the v3 image was built from
# (upstream cuts no release tags) — bump deliberately, same as the claude CLI.
# renovate: datasource=npm depName=bun
ARG BUN_VERSION=1.4.2
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
#
# ⚠️ HELD AT 0.14.6: 0.14.7 added "Set width/height on outgoing image
# attachments" (AsamK/signal-cli#2093), which reads the image through
# javax.imageio — and the GraalVM NATIVE build ships no ImageIO/AWT at all.
# So on 0.14.7 every outgoing image dies with "Can't load library: awt
# (UnsatisfiedLinkError)", or "Could not initialize class javax.imageio.ImageIO"
# when the type is declared. Text and non-image attachments are unaffected,
# which is why this only showed up the first time a photo was sent.
# Before bumping, send yourself a photo from chat and confirm it arrives as an
# image rather than a "MacBinary archive" file card. The JVM distribution
# (signal-cli-<v>.tar.gz + a Java 21 runtime) is the other way out, and would
# even shrink the image ~200MB, but it replaces the daemon this surface talks
# over — not worth it to recover a dimensions field.
#
# The pin alone was not enough: 0.14.7 had already run once, and it migrates
# account.db to schema 30 (0.14.6 tops out at 28). A downgraded daemon then
# refuses to start — "Database has been updated by a newer signal-cli version",
# CrashLoopBackOff on the signal-cli container only. Fixed by rolling the
# schema marker back on the PVC:
#   sqlite3 ~/.local/share/signal-cli/data/<acct>.d/account.db \
#     'PRAGMA user_version = 28;'
# Safe here because migrations 29/30 are purely additive (sticker storage-sync
# columns + recipient.pni_signature_verified, all NOT NULL DEFAULT or nullable)
# and every 0.14.6 INSERT names its columns. So the DB now carries 0.14.7's
# columns while claiming 28 — going FORWARD again would re-run 29/30 and die on
# "duplicate column name". Set user_version back to 30 before any bump past
# 0.14.6, and back it up first: k8up snapshots this PVC, but the Signal
# registration is not re-creatable without a fresh SMS verify.
# renovate: datasource=github-releases depName=AsamK/signal-cli extractVersion=^v(?<version>.+)$
ARG SIGNAL_CLI_VERSION=0.14.8
# The other coding agents. Each is a real CLI with its own agent loop and tools,
# not a model id handed to claude — the messaging gateway drives them over ACP
# (Agent Client Protocol) and Happy has had `happy codex` / `happy gemini` /
# `happy acp` since 1.2.0. Auth for all three is a one-time interactive login
# persisted on the home PVC (~/.codex, ~/.gemini, ~/.muse), the same shape as
# the claude/gh/tailscale logins — no API key is baked in or passed as env.
#
# NOT agy (the Antigravity CLI) for Google: it stores its OAuth token only
# through the org.freedesktop.secrets D-Bus API with no plaintext fallback
# (google-antigravity/antigravity-cli#57), and there is no dbus or keyring
# daemon here — nor a way to share one across the pod's containers. gemini-cli
# writes ~/.gemini/oauth_creds.json, a plain file on the PVC, and is the only
# one of the three that speaks ACP itself (`gemini --acp`).
#
# The other two need an adapter, and each needed a different one — verified
# against v32 rather than assumed, which is the only reason this is right:
# `codex` has no acp subcommand (it has `app-server`, its own JSON-RPC), and
# `muse` has `serve`, which is Meta's MSP rather than ACP.
# renovate: datasource=npm depName=@openai/codex
ARG CODEX_VERSION=0.155.1
# ACP server for codex, from the agentclientprotocol org (the protocol's own).
# renovate: datasource=npm depName=@agentclientprotocol/codex-acp
ARG CODEX_ACP_VERSION=1.12.0
# renovate: datasource=npm depName=@google/gemini-cli
ARG GEMINI_CLI_VERSION=0.60.0
# Muse Code (Meta). Upstream only documents `curl https://dev.meta.ai/install.sh
# | bash`, which drops a SELF-UPDATING launcher in ~/.local/bin — useless under a
# read-only rootfs and unpinnable besides. But the channel endpoint
# (api.meta.ai/muse-code/channels/muse-stable) and the manifest it points at are
# both public and unauthenticated, and the manifest hands out a direct versioned
# artifact URL plus its sha256. So we pin the binary like every other tool here.
# To bump: curl the channel endpoint for the current version, then its
# manifest_url for the new artifacts.x86_linux.checksum.
# ⚠️ ~300MB — by far the largest thing in this image.
ARG MUSE_VERSION=1.3.0-R3057.1
ARG MUSE_SHA256=5187dfc9e5b460999acb5c2326b09190d307e3fe09127490eb5de39a8b200309
# muse-acp is built in the stage at the top of this file; MUSE_ACP_VERSION is
# declared there. Re-declared here only so the COPY below can name it.
ARG MUSE_ACP_VERSION

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

# Muse itself. The checksum is verified rather than trusted: this comes off a
# lookaside CDN with no signature, and the manifest that publishes the digest is
# a different request from the one that serves the bytes.
RUN curl -fsSL -o /usr/local/bin/muse \
        "https://lookaside.facebook.com/lookaside/muse/download/?channel=muse&version=${MUSE_VERSION}&file=muse-x86-linux" \
 && echo "${MUSE_SHA256}  /usr/local/bin/muse" | sha256sum -c - \
 && chmod 0755 /usr/local/bin/muse
COPY --from=muse-acp-build --chmod=0755 \
     /src/target/release/muse-acp /usr/local/bin/muse-acp
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
# codex and gemini-cli are pinned like every other tool here; they are agents in
# their own right, not models, and both Happy and the messaging gateway select
# between them at runtime (see the agent ARGs above).
RUN npm install -g @anthropic-ai/claude-code@latest happy@1.2.0 "bun@${BUN_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
        "@agentclientprotocol/codex-acp@${CODEX_ACP_VERSION}" \
        "@google/gemini-cli@${GEMINI_CLI_VERSION}" \
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
# The secrets CLI, generated into the context by build.sh. On PATH as `secrets`
# so the pod can read and edit vault items with no checkout and no script path.
COPY --chmod=0755 secrets-cli /usr/local/bin/secrets

COPY --chown=1000:1000 gateway /opt/messaging-gateway
RUN cd /opt/messaging-gateway \
 && HOME=/root bun install --frozen-lockfile --production \
 && chown -R 1000:1000 /opt/messaging-gateway/node_modules \
 && rm -rf /root/.bun/install/cache

# The journal viewer (read-only web view of the trading journal checkout on the
# PVC, see journal/ and README "Journal surface"). Same shape as the gateway:
# in-repo bun app, deps resolved at build time, read-only at runtime.
COPY --chown=1000:1000 journal /opt/journal-viewer
RUN cd /opt/journal-viewer \
 && HOME=/root bun install --frozen-lockfile --production \
 && chown -R 1000:1000 /opt/journal-viewer/node_modules \
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
