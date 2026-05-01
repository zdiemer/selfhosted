# claude-bridge

Bridge that lets Minecraft players talk to Claude directly from in-game.
Players type `/claude <question>` (a Brigadier command registered by the
sibling [`claude-mod`](../claude-mod/) Fabric mod), the bridge tails the
server log for the dispatch line, runs the Claude Code CLI in a
sandboxed pod, and broadcasts the reply via RCON `tellraw`.

The bridge itself runs no Minecraft code — it streams the Minecraft pod's
stdout via the Kubernetes API and reaches RCON over its existing
ClusterIP service. Bridge upgrades never restart the Minecraft pod.
(Installing `claude-mod` does require one restart — see its README.)

## What it does

`/claude clear` (or `reset`) drops the player's cached conversation
context so the next `/claude` starts a fresh session. Useful when the
LLM has gotten stuck on a wrong assumption or carried too much history.



```
/claude what's the recipe for a beacon?
[Claude → Bob] Surround a Nether Star with 3 obsidian on the bottom row and
[…] 5 glass on top. Place on a 3×3 base of iron/gold/emerald/diamond blocks.
```

Plus: when a player expresses a feature wish, Claude calls a tiny MCP
tool that appends the request to `FEEDBACK.md`, commits, and pushes to
this repo.

Death advice: when a player dies, the bridge sees the vanilla death
message in the log stream (`<Name> was slain by …`, `<Name> burned to
death`, etc.) and whispers one piece of tailored advice to the dying
player only — the killer's weakness, an item to carry, terrain to
avoid. Runs in an ephemeral Claude session so it doesn't bleed into
their regular `/claude` context. Toggle via `bridge.deathAdvice.enabled`
in values.yaml.

Container reorganization is also wired up: when a player asks "organize
this chest" or "sort my inventory," Claude reads the current contents
via the bridge's MCP tools, computes a new layout, previews the diff in
chat, and — after the player confirms — applies it atomically through a
chunked write protocol on the sibling [`claude-mod`](../claude-mod/).
The mod owns server-authoritative safety (≤8-block distance, no hoppers
attached, no players viewing, item-conservation invariant, TOCTOU
contents-hash). The bridge persists the pre-commit state to the PVC
(last 5 snapshots per player) so `undo_container_reorg` can revert.
Targets supported: chests/barrels/shulker boxes, the caller's main
inventory (slots 9–35 only — hotbar/armor are unreachable), and
Travelers Backpacks (equipped or world-placed).

## Sandbox layers

1. **Pod hardening** — non-root, `readOnlyRootFilesystem: true`, all caps
   dropped, `seccompProfile: RuntimeDefault`, single tmpfs `/tmp`. State
   on a PVC, OAuth credentials read via a hostPath dir mount of the
   user's `~/.claude` (dir, not file — single-file mounts pin the
   inode and miss tmp+rename token refreshes).
2. **Tool allowlist** — `claude-config/settings.json` denies `Bash`,
   `Edit`, `Write`, `NotebookEdit`, `Task`. Only `Read`, `Glob`, `Grep`,
   `WebFetch`, `WebSearch`, and the feature-request MCP tool are
   allowed. Claude Code runs as a "researcher with web access," not a
   shell agent.
3. **NetworkPolicy** — egress restricted to DNS, RCON inside the
   namespace, the kube-apiserver, and `:443` on public IPs (for
   `api.anthropic.com` + `github.com`). Ingress: zero.
4. **Bridge guardrails** — per-player rate limit (default 5/60s), prompt
   char cap (500), response char cap (800), live whitelist enforcement
   from RCON `whitelist list`.
5. **Tightly scoped RBAC** — the ServiceAccount can only `get`/`list`/`watch`
   pods and `get` `pods/log` in the `minecraft` namespace. Nothing else.

## First install

```bash
# 1. Confirm you've run `claude login` on this host so
#    /home/zachd/.claude/.credentials.json exists. The bridge hostPath-
#    mounts the parent ~/.claude directory (UID 1000 on both sides,
#    mode 0600 on the file preserved).
ls -ln ~/.claude/.credentials.json

# 2. Build + side-load the image into k3s containerd.
./minecraft/claude-bridge/build.sh

# 3. Drop in the GitHub token (optional but recommended — without it,
#    feedback only lands in a local JSONL audit log on the PVC).
cp minecraft/claude-bridge/values.local.yaml.example \
   minecraft/claude-bridge/values.local.yaml
$EDITOR minecraft/claude-bridge/values.local.yaml

# 4. Install. The chart deploys into the existing `minecraft` namespace
#    so it can resolve mc-minecraft-rcon and read mc-rcon for the password.
helm install claude-bridge ./minecraft/claude-bridge -n minecraft \
  -f minecraft/claude-bridge/values.yaml \
  -f minecraft/claude-bridge/values.local.yaml
```

## Upgrade

```bash
./minecraft/claude-bridge/upgrade.sh           # values-only changes
./minecraft/claude-bridge/upgrade.sh --build   # also rebuild the image
```

## Tuning

Everything player-visible lives in `values.yaml` under `bridge:`:

| Knob | Default | Notes |
|---|---|---|
| Trigger | `/claude <prompt>` | Registered by `claude-mod`; not a values knob. |
| `bridge.maxPromptChars` | 500 | Rejected with a chat note above this. |
| `bridge.maxResponseChars` | 800 | Truncated with `…` above this. |
| `bridge.rateLimit` | 5 / 60s | Per-player sliding window. |
| `bridge.enforceWhitelist` | `true` | Pulled live from RCON. |
| `bridge.systemPrompt` | (chat-tuned) | Steers replies toward 1-2 short sentences. |
| `bridge.feedback.repoUrl` | this repo | Where `FEEDBACK.md` commits land. |

Logs:

```bash
kubectl -n minecraft logs -f deployment/claude-bridge
```

## Failure modes

- **Token expiry.** OAuth access tokens last ~8h and are refreshed by
  Claude Code in place via tmp+rename. The pod sees those refreshes
  because the hostPath mount is the `~/.claude` directory — a single-
  file mount would pin the original inode and the pod would go stale
  ~8h after every host-side refresh. As long as this pod or your host
  runs `claude` at least every refresh-token lifetime (~months), tokens
  stay fresh; if both sit idle that long, `claude login` again on the host.
- **Minecraft pod restarts.** Bridge re-resolves the pod by label and
  re-attaches; chat dropped during the cutover is lost (typical chat
  bridge behavior).
- **GitHub push conflict.** The MCP tool fetches + hard-resets to
  `origin/main` before each commit, so a stale local branch can't block
  pushes — but if main moves between fetch and push, the push fails and
  the request stays in the local JSONL audit log
  (`/app/state/feature-requests.jsonl` on the PVC). Replay manually if
  needed.
- **Claude refuses a question.** The system prompt asks for chat-shaped
  answers; if Claude responds with a refusal it's still chunked and
  broadcast as-is.

## Cost

Auth uses your Claude Pro/Max subscription via the hostPath-mounted
credentials, so player usage counts against your subscription quota
rather than metered API billing. To swap to metered API mode, replace
the `claude-creds` volume with a Secret holding `ANTHROPIC_API_KEY` and
add it to the container env.

## Caveats

- Single replica only; RCON broadcasts and FEEDBACK.md commits aren't
  parallel-safe.
- Pinned to `zachd-ubuntu` via `nodeSelector` because that's where the
  hostPath `~/.claude` directory lives.
