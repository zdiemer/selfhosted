# adguard

AdGuard Home as the DNS resolver for every device on the tailnet. It blocks ads
and trackers at the DNS layer and sends upstream queries over DoH (Quad9 +
Cloudflare). It needs nothing on the eero and nothing per device.

- DNS: `:53` UDP/TCP on **every node** (klipper-lb LoadBalancer `adguard-dns`)
- Admin UI: https://adguard.zachd.duckdns.org (Authelia)
- Namespace: `dns`, PSA **restricted**

## Turning it on for the tailnet (manual, once)

**In this order. The order matters.**

0. **Nodes off Tailscale DNS first:** `python3 infra/node-configuration/apply.py --apply`.
   Every node is itself a tailnet client. With "Override local DNS" on and the
   nodes still accepting Tailscale DNS, the nodes resolve through AdGuard too.
   containerd then can't pull AdGuard's image, and `k3s-resolv-conf.py` hands
   CoreDNS `100.100.100.100` as a pod upstream. That loop black-holed the whole
   tailnet's DNS on 2026-11-08.
1. `./upgrade.sh` and wait for it to pass. It only creates the :53
   LoadBalancer after the pods are Ready.
2. Only then, Tailscale admin console → **DNS**:

   - **Global nameservers → Add nameserver → Custom**, twice:
   - `100.71.229.116` (zachd-ubuntu-3)
   - `100.80.231.119` (zachd-ubuntu-5)
   - Enable **Override local DNS**.

MagicDNS keeps answering `*.ts.net` first; everything else goes to these
addresses. Two control-plane nodes are listed so that losing one doesn't take
DNS down. Any node address works, because klipper-lb forwards :53 from every
node to whichever replica is up. If you change the pair, also change
`CHECK_NODES` in `upgrade.sh`.

Do **not** add a public resolver (1.1.1.1) as a third entry "for safety".
Clients spread queries across all listed nameservers, so ads would leak
through on a share of lookups.

**Rollback:** remove the two nameservers in the console. Clients fall back to
their local DNS immediately. Nothing in the cluster needs undoing.

## Why it's built this way

**Stateless, two replicas.** Every tailnet lookup goes through this, so it is
built to survive a node loss rather than to keep history:

- config is re-rendered from `values.yaml` on every start (ConfigMap → init
  container → writable emptyDir)
- filter lists are re-downloaded on start (~5s, covered by the readiness delay)
- query log and stats are **in memory, per replica**, gone on restart
- `maxUnavailable: 0`, a PDB with `minAvailable: 1`, and a topology spread
  across nodes (not pinned to any node)

**One low-breakage list (HaGeZi Multi Light), on purpose.** The first
deploy used the AdGuard DNS filter + AdAway. They blocked hosts that pages
wait on (A/B testing, feature flags, consent tools), and ad-heavy sites stalled.
The rule now: pages must load without debugging each site. Blocked lookups get
NXDOMAIN (`blockingMode`), not `0.0.0.0`, which Safari tried to connect to.
For blocking ads inside pages, use a browser content blocker, not more DNS lists.

**The UI is not the source of truth.** Settings changed in the UI only last
until that pod restarts, and only on the replica that served the page. Put
upstreams, lists and allowlist rules (`userRules`) in `values.yaml` and run
`./upgrade.sh`.

**No AdGuard login.** `users: []` turns off AdGuard's own auth. Authelia
forward-auth is the only gate, and the NetworkPolicy admits the UI port from
Traefik only. `upgrade.sh` fails if an anonymous request gets through.

**Every client looks the same.** CNI host-port forwarding and klipper-lb both
rewrite the source address (`games/terraria` found this), so AdGuard sees one
svclb address for every device. The consequences:

- `ratelimit: 0`. A per-client limit would throttle the whole tailnet as
  one client.
- Per-client stats and per-client rules don't work.
- `loadBalancerSourceRanges: 100.64.0.0/10` would match nothing, so there are
  none. **LAN hosts can use the resolver too.** That's harmless: the router
  forwards no :53, so it is not an open resolver on the internet.

**Unprivileged.** It listens on 5353 inside the pod (the Service maps 53 →
5353) as uid 65534 with a read-only root filesystem. `NET_BIND_SERVICE` stays
in the bounding set only because the image setcaps the binary.

**Egress** is public :443 and :53 plus cluster DNS. No LAN, tailnet or cluster
services.

## Upgrading AdGuard

`schema_version: 34` in `templates/configmap.yaml` must match the binary
(`internal/configmigrate.LastSchemaVersion`). When Renovate bumps the image,
check that constant at the new tag. A newer binary migrates an older schema on
every start, which works but is noisy. An older binary refuses a newer schema.

## Deploy

```bash
./upgrade.sh     # creates `dns` + applies pod-security labels on first run
```

Two phases. Phase 1 installs with `dns.loadBalancer=false` (pods only) and
waits for Ready. Phase 2 adds the LoadBalancer. If phase 1 fails, nothing has
claimed :53 on any node. Upgrading a running release keeps its LoadBalancer
throughout.

It checks that `example.com` resolves and `pagead2.googlesyndication.com` returns
NXDOMAIN via both nameserver nodes, and that the UI rejects anonymous
requests.
