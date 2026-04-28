# claude-mod

Tiny server-side Fabric mod that registers a `/claude <prompt>` Brigadier
command. The command logs `[ClaudeRequest] <player>: <prompt>` to server
stdout — the sibling [`claude-bridge`](../claude-bridge/) sidecar tails
the pod log and runs the prompt through Claude Code.

## Why a mod (and not just `!claude` in chat)

The original chat-prefix trigger went through public chat, which
`dcintegration` relays to Discord. Every prompt spammed the channel.
`/teammsg` would have bypassed Discord, but vanilla doesn't echo team
chat to server stdout, so the log-tail bridge can't see it.

A Brigadier command is the only no-restart-able-once-installed path
that's both (a) logged to stdout and (b) not relayed by dcintegration's
chat listener. Permission-gateable via `claude.use` through LuckPerms,
tab-completes for free, server-only (vanilla clients work).

## Install

```bash
./minecraft/claude-mod/install.sh
```

This:
1. Builds the JAR in a one-shot `gradle:8-jdk17` container (no local
   Java needed; ~200MB of gradle/loom downloads on first run, cached
   in `~/.gradle-claude-mod` afterwards).
2. `kubectl cp`s `build/libs/claude-mod-<version>.jar` into the
   Minecraft pod's PVC at `/data/mods/claude-mod.jar`.
3. Flushes the world via RCON.
4. **Restarts the Minecraft pod** — players are disconnected.

itzg's image preserves manually-placed JARs in `/data/mods` across
boots, so the JAR survives subsequent `helm upgrade`s without
re-running install. The `MODS` env var in `minecraft/values.yaml` is
**not** modified — this is a fully local sideload.

## Upgrade

Bump `mod_version` in `gradle.properties`, then re-run `install.sh`.
The cp overwrites the existing JAR; the restart picks up the new one.

## Permissions

Default: everyone allowed (permission level 0). To gate via LuckPerms:

```bash
./minecraft/rcon.sh "lp group default permission set claude.use false"
./minecraft/rcon.sh "lp group trusted permission set claude.use true"
```

## Layout

```
build.gradle             # fabric-loom build, versions sourced from gradle.properties
gradle.properties        # mod_version + minecraft/loader/yarn/fabric-api versions
settings.gradle          # adds maven.fabricmc.net plugin repo
install.sh               # build + sideload + restart
src/main/java/com/zachd/claudemod/
  ClaudeMod.java         # ModInitializer; registers the command
  ClaudeCommand.java     # Brigadier registration + handoff to stdout
src/main/resources/
  fabric.mod.json        # mod metadata, server-only, depends fabric-permissions-api
LICENSE
```

## Versions

Pinned in `gradle.properties`. When the modpack moves to a newer
Minecraft / Fabric Loader, bump these in lockstep:

| Knob | Current |
|---|---|
| Minecraft | 1.20.1 |
| Fabric Loader | 0.15.11 |
| Yarn mappings | 1.20.1+build.10 |
| Fabric API | 0.92.2+1.20.1 |
| fabric-permissions-api | 0.3.1 (compileOnly — runtime is the cluster's existing install) |

## Caveats

- **Restart required to install or upgrade.** Schedule when the server
  is empty.
- **The protocol is the printf format.** If you change the log line in
  `ClaudeCommand.run()`, also update `COMMAND_RE` in
  `../claude-bridge/src/bridge.py`.
- The mod depends on the runtime presence of `fabric-permissions-api`,
  which is already installed via `MODRINTH_PROJECTS` in
  `minecraft/values.yaml`. If you ever drop that, this mod stops loading.
