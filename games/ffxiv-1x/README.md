# ffxiv-1x — FINAL FANTASY XIV 1.23b, the original game

The 2010 release of FFXIV — the one the Calamity erased when A Realm Reborn
replaced it in 2012 — running as a private server. The server is
[Garlemald Server](https://github.com/swstegall/Garlemald-Server), a Rust port
of the long-running C# [Project Meteor](https://bitbucket.org/Ioncannon/project-meteor-server)
emulator that collapses Meteor's MySQL + PHP + IIS stack into four binaries and
one SQLite file, and drives the same 1,142 Lua content scripts.

```
launcher ──HTTP 54993──▶ web    ─┐ one pod   ┌─ data PVC (garlemald.db, logs)
client   ──TCP  54994──▶ lobby  ─┤ localhost ┤
client   ──TCP  54992──▶ world  ─┼───────────┴─▶ map :1989 (pod-internal)
                                  └─ scripts/lua (image)
```

## What works, honestly

Upstream's own words (August 2026): account flow, character creation and
select, zone loading, NPC and monster spawns, chat, movement, guildleves and
status effects work end to end against a retail 1.23b client. Combat damage,
stat recalculation, level/XP, inventory events and the quest-reward pipeline
are partial. The opening sequences of all three cities and their first story
arcs are scripted; the rest of the quest catalogue is not. See
[`CONTENT.md`](CONTENT.md) for the quest-by-quest inventory this chart was
deployed with, and what restoring the rest involves.

It is a place to walk around 1.0 Eorzea — the cities, the fields, the
aetherytes, the chocobo, the red moon — and to play the opening. It is not
yet a game you can level in.

## Why it is built the way it is

**Garlemald, not Meteor.** Meteor is the reference, still maintained on
Bitbucket (last substantive commit October 2025), but it is .NET Framework
against MySQL with a PHP login page. Garlemald is the same server in Rust with
SQLite embedded and the login page in the binary, moves faster (commits
weekly), and runs from one image with no database to operate. Its content
scripts are Meteor's, so nothing is lost by choosing it.

**Built from upstream git at a pinned commit.** No release is current for
long; `image.upstreamRef` is a full sha and `build.sh` passes the repo as the
build context. The Dockerfile is ours (upstream has none).

**One pod, four containers.** Exactly upstream's `run-all.sh`: the four
binaries share `garlemald.db` and talk on `127.0.0.1`. The world server dials
the map server, and relays zone traffic to the client, so the map port is
never exposed.

**Three LoadBalancer ports, no Ingress.** The launcher's login WebView posts
to the web port over plain HTTP and the lobby hands the client the world's
*IP* in a packet; neither can ride Traefik. So the three client-facing ports
are published on the node IPs by klipper-lb, like `minecraft/` and
`games/smt-imagine`, reachable on the tailnet only.

**The lobby host is an IP, and that is not a preference.** The launcher
writes the lobby hostname into a 20-byte slot in `ffxivgame.exe`.
`ffxiv.zachd.duckdns.org` does not fit; `100.84.179.82` does. The same
address is `world.advertisedIP` in values.yaml.

## Deploy

1. `./build.sh` — ~8 minutes cold on the in-cluster buildkitd.
2. `./upgrade.sh`.
3. Create an account from the launcher's sign-up form (it opens the web
   port). There is no seeded admin and no secrets in this chart.

## Client setup

You need the retail 1.0 install — the DVD or its ISO with `ffxivsetup.exe`
at the root. Square Enix never re-released it. archive.org holds a rip of the
Windows disc (item `ffxiv-1.0`, a 4.9 GB `FFXIV 1.0.rar`, uploaded 2021) and
a Redump-style dump of disc 2 of the JP release (`Nova_FFXIVOnlineDisc2_JPN`,
disc 1 missing); the physical disc is still the clean source and is cheap.
Everything after that is automated:

1. Install the base game (`2010.09.18.0000`). On Linux or a Mac, upstream's
   [XIV-1.0-Linux-Installer](https://github.com/swstegall/XIV-1.0-Linux-Installer)
   / Apple Silicon installer drive the InstallShield setup under Wine.
2. Run [Garlemald Client](https://github.com/swstegall/Garlemald-Client). It
   finds the install, downloads and applies the patch chain to `1.23b`
   (`2012.09.19.0001`, CRC-verified; upstream mirrors the patches), and
   shows a server dropdown.
3. Add this server. Put a `servers.toml` next to the launcher's config:

       [[server]]
       name = "Diemer (tailnet)"
       address = "100.84.179.82"
       login_url = "http://100.84.179.82:54993/login"

   Sign up, log in, make a character, pick Fernehalwes.

## Operations

- **Save game** is `garlemald.db` on the data PVC; in k8up's scope.
- **GM commands** go to the map server's stdin upstream. Here:
  `kubectl -n games attach -it deploy/ffxiv-1x -c map` and type them.
- **Logs**: `kubectl logs -c {web,lobby,world,map}`; `RUST_LOG` is
  `logging.rustLog` in values.yaml.
- **Upgrading**: pick a new upstream commit, set `image.upstreamRef` and
  `image.tag` (`main-<short sha>`) plus `appVersion`, `./build.sh`,
  `./upgrade.sh`. Renovate cannot see this image.
