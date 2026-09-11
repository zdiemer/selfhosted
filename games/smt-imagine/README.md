# smt-imagine — Shin Megami Tensei IMAGINE, solo

The 2007 MMO (Western servers closed 2014, Japan 2016), running on the cluster
as a private server for one. Server software is
[COMP_hack](https://github.com/HyperChiicken/SMT) 4.12.2 "Wyrd", the
clean-room emulator the community built after shutdown; this chart runs its
three daemons in one pod against SQLite, the same shape as the Debian package's
three systemd units on one Ubuntu host.

```
ImagineClient.exe ──TCP 10666──▶ lobby   ─┐             ┌─ database PVC (SQLite, logs)
                  ──HTTP 10999──▶ lobby   ─┤ one pod,   ─┤
                  ──TCP 14666──▶ channel ─┘ localhost  ◀┴─ world
                                            ▲
                   datastore (in the image) ─┤
            client BinaryData + maps (PVC) ──┘
browser ──HTTPS──▶ Traefik + Authelia ──▶ lobby :10999 /accountmanager/
```

## Why it is built the way it is

**The source is a tarball, not a repo.** `github.com/comphack` is an empty
organisation now. The maintained fork, `HyperChiicken/SMT` (last commit April
2022), still has the top-level tree but its `libcomp` and `datastore`
submodules point at repositories that no longer exist — and those are the core
library and all of the game content. The only complete copy left is the source
package behind the old Ubuntu PPA, which vendors both:
`comphack_4.12.2.tar.xz` (28 MB, 179 libcomp sources, 2,944 datastore files).
The `Dockerfile` fetches it by URL with a pinned sha256. If Launchpad ever drops
it, `infra/registry` holds the built image.

**bullseye, on purpose.** The project targets Ubuntu 16.04 / GCC 5 and the
top-level CMake insists on Qt5 Widgets even for a server-only build; bullseye
is the newest Debian that still ships `libqt5webkit5`. The build flags are
`debian/rules` from the tarball, verbatim. Runtime image carries only the three
servers, the `comp_encrypt`/`comp_decrypt`/`comp_rehash` tools, the datastore
and the account-manager webroot — no Qt.

**Client data is a PVC, not a layer.** The server reads two trees straight out
of the game client and ships neither: `BinaryData/` (every item, skill, demon,
zone definition) and `Map/Zone/Model/*.qmp` (zone geometry). They are Atlus
copyright, so they stay out of git and out of the registry; `stage-client.sh`
copies them from a client directory onto the `-data` claim once. The channel
will start without them and then refuse every zone, so `upgrade.sh` warns when
the claim is empty.

**One pod, three containers.** Lobby, world and channel find each other on
`127.0.0.1` exactly as the package configured them, and share the SQLite
directory the way they shared `/var/lib/comp_hack`. World is never exposed;
lobby and channel are what the client connects to.

**Game ports on the node IPs.** The client connects to the lobby on raw TCP
(an unpatched client first POSTs its login to the lobby's *HTTP* port), and
is then handed the channel's address as a string. None of that can ride Traefik, so the three ports are a
`LoadBalancer` published by klipper-lb on every node, exactly as
`minecraft/` does with 25565. `infra/duckdns` resolves `*.zachd.duckdns.org`
to a node's tailnet address, so `smt.zachd.duckdns.org:10666` works from any
tailnet device — and `channel.externalIP` in values.yaml is that same node
address, which is the one non-obvious setting: the lobby sends the client
this string verbatim, so it must be an IP the client can reach. There is no
Cloudflare host and there will not be one.

## Deploy

1. `./build.sh` — compiles the image and pushes
   `registry.zachd.duckdns.org/zdiemer/smt-imagine:<tag>`. ~10 min the first
   time on the in-cluster buildkitd.
2. `secrets new games/smt-imagine` — one value, the GM account's password
   (see `values.local.yaml.example`). It seeds the first account at the lobby's
   first start, GM level 1000, one million CP; changing it later does nothing.
3. `./upgrade.sh`.
4. `./stage-client.sh /path/to/ReIMAGINE` — the directory with
   `ImagineClient.exe`. Then `kubectl -n games rollout restart deployment/smt-imagine`.
5. Account manager: <https://smt.zachd.duckdns.org/accountmanager/> (Authelia,
   then the game account). Set your user level / CP here, create more accounts.

## Client setup

The client is the archive.org `SMTREIMAGINE` bundle (ReIMAGINE, 6 GB,
reports version 2.032 — `client.version` in values.yaml must match). Two files
in its root point it at a server:

`ImagineClient.dat`:

    -ip smt.zachd.duckdns.org
    -port 10666

The ReIMAGINE client ships with `comp_client.dll` and a `comp_client.xml`
that applies the `noWebAuth` patch, so it logs in over the lobby's TCP port
directly and never POSTs to the web port — `ImagineClient.dat` alone is
enough to play. `webaccess.sdat` only matters for the in-game web panels
(and for an unpatched client). It is encrypted; write the plaintext as
`webaccess.dat` with the `login` line pointing at the web port, then encrypt
it with the tool in the image:

    <login = http://smt.zachd.duckdns.org:10999/>
    <dbnet = http://smt.zachd.duckdns.org:10999/index/auth?user_id=%s&user_password=%s&character_name=%s&world_id=%d>
    … (the rest of the lines as in the stock file; they are in-game web panels)

    kubectl -n games exec deploy/smt-imagine -c lobby -- comp_encrypt /dev/stdin /dev/stdout < webaccess.dat > webaccess.sdat

`make-client-config.sh` does both and leaves the two files ready to drop into
the client directory. Windows only (or Wine); the machine needs to be on the
tailnet.

## Playing alone

The game was built around you plus one demon; story acts, expertise, fusion
and the instanced dungeons were soloable on retail. What does not work alone:
the party raids and the player economy. The seeded account is GM level 1000,
which unlocks the `@` commands in chat (`@item`, `@level`, `@xp`, `@zone`,
`@spawn` …), and `world.bonus.*` in values.yaml scales XP/expertise/drops
server-wide. It is a single-player SMT with an MMO skin; treat it as one.

## Operations

- **Backups.** The `-database` PVC is the save game and is in k8up's scope;
  `-data` is annotated `k8up.io/backup: "false"` and is re-staged instead.
- **Logs.** `/var/log/comp_hack/{lobby,world,channel}.log` on the database PVC,
  rotated daily ×3; `kubectl logs` carries the same lines (stdout hook).
- **Config changes** restart the pod (checksum annotation); the servers read
  `/etc/comp_hack` once at start.
- **Renovate** cannot see this image: upstream will never release again. The
  base image pins (`debian:bullseye-slim`) are the only thing that moves.
