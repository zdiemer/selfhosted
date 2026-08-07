# bluemap-ingress

Publishes the Minecraft world's BlueMap web viewer at
`https://map.zachd.duckdns.org`.

This chart is **one Ingress and nothing else**. The workload it points at —
`mc-minecraft-bluemap`, a webserver running inside the Minecraft pod — belongs
to the upstream `itzg/minecraft` chart (release `mc`), which knows nothing about
this repo's ingress conventions. Same arrangement, and the same reasoning, as
[`web/talaria-deals`](../../web/talaria-deals).

```
browser ──TLS──▶ Traefik ──Host: map.zachd.duckdns.org──▶ mc-minecraft-bluemap:8100
                    │
               duckdns certresolver, riding the *.zachd.duckdns.org wildcard
```

## Why it's a chart now

It used to be `minecraft/bluemap-ingress.yaml`, applied with a bare
`kubectl apply -f`. That made it the only Ingress in the cluster with no Helm
ownership at all: absent from `helm list`, untouched by any upgrade, and with no
way to roll it back. Its header comment had also drifted badly — it still
credited "the talaria duckdns-updater CronJob" and
`talaria/helm/traefik-config.yaml`, neither of which had existed for months.

The live object was **adopted in place**, not recreated: `upgrade.sh` stamps the
Helm ownership annotations onto it before the first install, so the route never
dropped. The rendered spec was verified byte-identical to the live one first.

## The release is named `bluemap`, not `bluemap-ingress`

The pre-existing object is named `bluemap`, and the chart's `fullname` helper
returns the release name. Naming the release to match is what let Helm adopt it
rather than create a second Ingress for the same host. `upgrade.sh` defaults to
`RELEASE=bluemap`; don't rename it without a plan for the live object.

## Deploy

```bash
./upgrade.sh          # installs into the `minecraft` namespace
```

Nothing here restarts the Minecraft server — this chart never touches the `mc`
release. It pre-flights that `mc-minecraft-bluemap:8100` exists before applying,
so a rename upstream fails loudly here instead of quietly serving 503s, then
probes Traefik through the real Host header to prove routing works end to end.

A `502`/`503` from that probe means routing is fine but BlueMap isn't serving —
almost always because it isn't enabled in the pod. It needs
`accept-download: true` in `/data/config/bluemap/core.conf` and a pod restart.

## Public on purpose

No forward-auth. BlueMap is a read-only render of the world — no commands, no
player data beyond positions — and gating it behind Authelia would mean handing
a login to everyone who plays. If that ever changes, add a middleware the way
[`docs/stirling-pdf`](../../docs/stirling-pdf) does.

## Known: BlueMap also bypasses Traefik entirely

`mc-minecraft-bluemap` is a **LoadBalancer** service, so klipper-lb publishes it
on port `8100` of *every* node IP. Anyone on the LAN can reach BlueMap directly
without passing through Traefik — which also means those requests never appear
in the access log.

The fix is `service.type: ClusterIP` in the minecraft values, but that is the
upstream chart and a `helm upgrade` there restarts the server. Do it in an
offline window, not while people are playing.
