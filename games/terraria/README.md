# Modded Terraria

Calamity on **tModLoader 2026.08.3.0 / Terraria 1.4.4.9**, with a large
Corruption world in Classic difficulty, eight player slots, and a Claude
companion. No Revengeance, Death or Infernum mode. The server is a separate
release in `games`; it does not modify Minecraft.

## Join

1. Install **tModLoader** in Steam alongside your owned copy of Terraria.
   Launch tModLoader, rather than vanilla Terraria. Match the server's
   `2026.8.3.0` version; its upstream release is linked below.
2. Subscribe to the Workshop mods in the table, enable them, and reload.
   The server can sync missing gameplay mods on join, but preinstalling the
   large Calamity/music downloads makes the first connection quicker.
3. Connect to the tailnet (or home LAN), then use **Multiplayer → Join via IP**:
   `terraria.zachd.duckdns.org`, port **7777**. There is no server password;
   the Service and NetworkPolicy limit clients to the LAN and tailnet.
4. Make a fresh **Classic/softcore character** for the shared adventure.
   Bind Recipe Browser, Boss Checklist and Ore Excavator keys in Controls.

The hostname uses the existing wildcard DNS record pointing at `zachd-ubuntu-2`.
On the home LAN you can also use `192.168.4.26:7777`. With
`externalTrafficPolicy: Cluster`, any node forwards to the game pod on
`zachd-ubuntu`, where the existing Claude credentials live. Terraria uses TCP;
Risk of Rain 2's UDP 7777 is independent.

The Service also permits the primary and wildcard entry nodes' bridge gateways
(`10.42.0.1/32`, `10.42.9.1/32`): CNI host-port forwarding translates tailnet
sources before klipper-lb evaluates its source ranges. These are explicit
translated entry paths, not an allowance for the whole pod network. Update
them if those nodes' pod CIDRs change. Pod egress policies still constrain
which applications can reach node/LAN destinations. The game NetworkPolicy
admits only this Service's labelled klipper-lb pods on TCP 7777 for the final
forwarded hop; it does not admit arbitrary `kube-system` pods.

| Mod | Purpose |
|---|---|
| [Calamity](https://steamcommunity.com/sharedfiles/filedetails/?id=2824688072) | Main adventure: bosses, biomes, gear and progression past Moon Lord |
| [Calamity Music](https://steamcommunity.com/sharedfiles/filedetails/?id=2824688266) | Calamity's soundtrack and music boxes; declared dependency |
| [Magic Storage](https://steamcommunity.com/sharedfiles/filedetails/?id=2563309347) | Searchable shared storage and crafting |
| [absoluteAquarian Utilities / SerousCommonLib](https://steamcommunity.com/sharedfiles/filedetails/?id=2908170107) | Required Magic Storage library |
| [Recipe Browser](https://steamcommunity.com/sharedfiles/filedetails/?id=2619954303) | Search recipes and item uses in game |
| [Boss Checklist](https://steamcommunity.com/sharedfiles/filedetails/?id=2669644269) | Boss order, progression and encounters |
| [Ore Excavator](https://steamcommunity.com/sharedfiles/filedetails/?id=2565639705) | Vein mining |

[`client-pack/`](client-pack/) is the matching tModLoader mod-pack export:
`Mods/install.txt` lists Workshop IDs and `Mods/enabled.json` lists internal
names. Copy its contents into a named folder under the client's
`tModLoader/ModPacks` directory to import it through Workshop → Mod Packs.
Alternatively subscribe through the links above. The custom Claude mod is
server-only and does not need a client install.

## Claude in game

```text
/claude what should we fight next with my current equipment?
/claude how do I craft a storage heart?
/claude teleport me to Alice
/claude take me home
/claude take me back
/claude clear
```

Claude can read live world/boss progression (including Calamity), online
players, the caller's inventory/equipment, and actual loaded recipes with
ingredients, alternatives, crafting stations and conditions. It can research
mod mechanics on the web and remembers conversations across requests and
pod restarts. `clear` or `reset` drops the caller's conversation.

Teleport tools only move the caller. They reject dead players, blocked
destinations and active boss encounters. Home uses a valid bed or world spawn;
back reverses the previous teleport during this connection. There are no item
spawning, administrative or inventory mutation tools in this release.

The mod handles `/claude` on the server and grants a random, six-minute
capability tied to the player's original network socket. All game reads and
mutations execute on the game thread; a reused player slot cannot inherit a
pending turn. IPC uses a private shared memory volume. Nothing listens on a
control port and neither container receives a Kubernetes API token.

Requests are capped at 500 characters, five per minute per connection, one
pending request per connection, and sixteen pending globally. The bridge runs
three workers, with a five-minute turn timeout and up to four 240-character
reply messages. Only the asking player sees the reply. Claude gets web tools
and the five explicit game tools; shell, file access, editing and delegated
agents are denied.

Auth follows the Minecraft bridge: Claude Code reads the existing
`/home/zachd/.claude` directory through a credential symlink, so atomic token
refreshes remain visible. Usage counts against that login's subscription.
If authentication expires, run `claude auth login` on `zachd-ubuntu`.
No game/server restart is needed after login. Names identify conversations;
Terraria character names are not authenticated Minecraft-style UUIDs, so
use this on the trusted tailnet and avoid sharing character names.

The games admission guard permits the exact credential directory only under
the `terraria` application label and `terraria-claude` service account, mounted
by the `claude` container at `/host-claude`. The game container cannot see it.
See [the guard source](../../infra/pod-security/guards.py).

## Build and operate

```bash
./games/terraria/build.sh
./games/terraria/upgrade.sh
kubectl -n games logs deployment/terraria -c server -f
kubectl -n games logs deployment/terraria -c claude -f
```

Builds compile the custom server mod using the pinned official tModLoader
release (SHA-256 checked), fetch Workshop mods, choose the newest compatible
build for tModLoader 2026.08, and bake that set into the image. Pod restarts
never update mods. [`mods.lock.json`](mods.lock.json) records the Workshop
builds and checksums used for this release. `build.sh` compares the built
image's `/opt/mods/lock.json` with this record and refuses to push if Workshop
content changed. Review the new record and bump both image tags
and `Chart.yaml` appVersion when deliberately updating. Steam clients can update
independently; keep the server/client pack versions aligned.

The server's root filesystem is read-only. `/tmp` must allow executable
mappings because MonoMod loads a native helper there; Kubernetes disk
`emptyDir` provides that. For local Docker tests, specify
`--tmpfs /tmp:exec,uid=1000,gid=1000` explicitly.

One 16 GiB `truenas-iscsi` claim stores `/server` (worlds, mod copies and
configs) and `/claude` (sessions and CLI state). The Recreate rollout allows
one writer. SIGTERM requests Terraria's console `exit`, which saves the world;
the pod has a 180-second termination grace period. A TCP startup probe allows
up to thirty minutes for first generation. Changing world-generation settings
does not regenerate an existing world.

The existing Tuesday `games` K8up schedule includes this PVC. A running-volume
backup is crash-consistent; it is not an application-consistent world snapshot.
Before a mod update or a manual world archive, stop the deployment gracefully
and preserve **both** `Calamity.wld` and `Calamity.twld` from the same save.
Restore both together, with the matching mod set; `.bak` files are the previous
save pair, not a separate off-volume backup. Never delete/regenerate the world
to fix a startup failure without first preserving the pair.

## Validation

```bash
helm lint games/terraria
helm template terraria games/terraria | python3 scripts/lib/check-availability.py games/terraria
python3 -m unittest discover -s games/terraria/tests -v
```

The initial container smoke test loaded the full pack, generated a Calamity
world, listened on TCP 7777, and saved/exited with status zero under a read-only
root. A real Claude login/MCP test exercised context and recipe queries,
chat replies, persisted conversations and reset against simulated game IPC.
A real-player join, live inventory query and teleport check still require an
in-game client; the simulated IPC test does not establish those behaviors.

Upstream: [pinned tModLoader release](https://github.com/tModLoader/tModLoader/releases/tag/v2026.08.3.0),
[official dedicated server utilities](https://github.com/tModLoader/tModLoader/tree/stable/patches/tModLoader/Terraria/release_extras/DedicatedServerUtils),
[official Calamity wiki](https://calamitymod.wiki.gg/wiki/Main_Page).
