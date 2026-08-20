# network-segmentation

**A plan, not a deployment.** Nothing here is installed and there is no chart —
this is the design for splitting the flat home LAN into segments, so that the
cluster and the SMS relay handset stop sharing a broadcast domain with the
laptops and phones.

Status: **not started.** Blocked on hardware, and on the open questions at the
bottom.

It lives in this repo because the cutover changes things in this repo
(`democratic-csi` targets, the sms-relay gateway URL, a new LAN NodePort) and
because the constraints that shaped it — svclb answering on all seven node IPs,
DuckDNS in `tailnet` mode, iSCSI throughput — are cluster facts, not networking
trivia.

## Why

Every device in the house is on one flat `192.168.4.0/22`: seven k3s nodes, the
NAS, the personal laptops and phones, the TVs, and the Android handset that
`infra/sms-relay` drives. Nothing is stopping a compromised pod that escapes to
its node from reaching a personal machine, and nothing is stopping the handset
— a budget Android running a third-party HTTP server on `:8080` — from reaching
etcd or the NFS exports.

Two things are deliberately *not* goals:

- **Isolating the NAS.** It is the shared thing. It must stay reachable by
  direct IP from both the cluster and personal devices, without Tailscale in
  the path, because NFS and iSCSI throughput is the point of it.
- **Isolating nodes from each other.** Flannel VXLAN and etcd want one L2
  segment with low latency. All seven nodes stay together.

## Current state, as measured

| | |
|---|---|
| LAN | `192.168.4.0/22`, gateway `192.168.4.1` |
| IPv6 | GUA `2001:5a8:4a45:6100::/64` (Sonic) + ULA `fd35:ace4:a929:1::/64`, router at `::1` on both |
| Nodes | `.26 .28 .30 .31 .32 .37 .39` — all wired, single NIC, all negotiating 1000BASE-T |
| NAS | `192.168.4.36` (TrueNAS SCALE) — NFS, iSCSI, SMB |
| Handset | `192.168.4.24`, wifi |
| Router | eero Max 7 |
| Switch | unmanaged |

The switch is unmanaged by observation, not assumption: `zachd-ubuntu` and a
second node on a different switch port both receive the *same* LLDP frame, same
chassis, same port —

```
Chassis ID: 8c:dd:0b:c8:fd:40
System Name: eero
System Description: eero Max 7
Port Description: eth1
Capabilities: [Bridge, WLAN AP, Router]
```

Two nodes seeing one eero port means something in between is forwarding LLDP
multicast rather than terminating it.

## The blocker

**Consumer eero cannot do any of this.** No 802.1q, no multiple subnets, no
per-SSID VLAN mapping, no inter-VLAN firewall rules, and no static routes.
Bridge mode does not help — it turns the eeros into APs on one flat L2 and
disables the guest network on the way out. VLAN support exists in eero for
Business; not in this line. There is no firmware or app setting that gets
there.

So this needs hardware, and the unmanaged switch has to go with it.

## Hardware

| Part | Choice | Note |
|---|---|---|
| Gateway | UniFi Cloud Gateway Ultra (~$129) or Max (~$199) | VLANs, inter-VLAN firewall rules, per-VLAN IPv6 from a delegated prefix. RB5009 (~$219) or OPNsense on a 2-NIC mini-PC are equivalent-capability alternatives. |
| Switch | 802.1q managed, ~14+ ports | Seven nodes, the NAS trunk, the handset, the eero uplinks. UniFi Standard 16 (~$179), or a pair of TL-SG108E at ~$25 each. |
| Wifi | Keep the eeros, in bridge mode | Uplink port tagged to VLAN 20. All wifi becomes trusted. Loses eero's guest network and app features. |
| Handset | USB-C → Ethernet (~$15) | See below — this is the piece that makes VLAN-capable APs unnecessary. |

Nodes are all 1000BASE-T, so LAN-side multi-gig buys nothing today. Size the
gateway to the WAN plan, not to the eero Max 7's 10G ports.

## The design

Three segments. The cluster keeps the subnet it already has — **renumber
people, not nodes**. Personal devices are DHCP and do not care where they live;
the cluster has seven node addresses, NFS exports pinned by IP, svclb bound to
every node address, and etcd.

| VLAN | Subnet | Members |
|---|---|---|
| 1 (existing, untagged) | `192.168.4.0/22` | seven k3s nodes + NAS |
| 20 trusted | `192.168.20.0/24` | laptops, desktop, phones, TVs, bridged eeros |
| 30 untrusted | `192.168.30.0/24` | the SMS relay handset |

Because VLAN 1 is left alone, `democratic-csi`, `k8up`, and Jellyfin's NFS
config need no changes at all.

### Everything isolated is wired

This is what keeps the bill down. The cluster and NAS are already on copper;
put the handset on a USB-C ethernet adapter into a VLAN 30 switch port and
**wifi never has to be segmented**, which is the only reason bridged eeros are
an acceptable AP tier.

Wiring the handset is also a reliability win on its own terms.
`infra/sms-relay/README.md` opens by describing it as "asleep, off wifi, or
between IP addresses more often than you'd like" — that is the stated
justification for the whole durable-outbox layer. Ethernet removes two of those
three failure modes, and the phone is already sitting on a charger.

## The NAS, on both subnets

One physical NIC, a **trunk port**, two L3 interfaces:

- `192.168.4.36` — untagged VLAN 1, unchanged, what the cluster uses
- `192.168.20.36` — tagged VLAN 20, what personal devices use

Both addresses are L2-local to their own segment, so neither path traverses the
gateway. That is the entire reason for doing it with a trunk rather than a
firewall rule: NFS and iSCSI never hit the router's forwarding plane.

Two rules make it behave:

1. **Exactly one default gateway**, on VLAN 1. A second default route on VLAN
   20 produces asymmetric returns that look fine at idle and come apart under
   load.
2. **Never route between VLAN 1 and VLAN 20 at the gateway.** Each client stays
   on-subnet with the address it uses. A gateway that will happily forward
   between them has silently rebuilt the flat network this is meant to replace.

Bind per-service listeners while you are in there:

| Service | Interface | Why |
|---|---|---|
| iSCSI | VLAN 1 only | `democratic-csi` is the only consumer; nothing on the trusted side should reach raw zvols. |
| TrueNAS web UI | VLAN 20 only | Keeps the NAS's own admin plane out of reach of a compromised pod. |
| SMB | both | smitele-bot and RomM mount shares from the cluster side; personal devices use the trusted side. |
| NFS | VLAN 1 only | Cluster-only by usage today. |

### The tradeoff this accepts

The NAS becomes the one box straddling both segments, so it is the bridge if it
is ever compromised: a hostile pod that reaches `192.168.4.36` and escalates on
the NAS lands on the personal VLAN. That is inherent in "reachable by direct IP
from both subnets" and it is the right call for throughput — the interface
bindings above are most of what limits it. Named here so it stays a decision
rather than a surprise.

## The handset, and the two holes

The NAS trick does not transfer: Android has no 802.1q and no host firewall
worth trusting, so **where it is plugged in is the only control available**. It
gets routed through the gateway, which is fine — this is a handful of small
JSON POSTs a day, not storage traffic.

| Direction | Rule |
|---|---|
| Sends | `VLAN 1 → 192.168.30.24:8080/tcp` |
| Inbound SMS | `192.168.30.24 → <node-ip>:3XXXX/tcp` (webhook NodePort) |

No NAS access. That is the whole blast radius.

### Why it is not on VLAN 1

Parking it with the cluster would save those two rules, and it is the reasonable
scope cut if inter-VLAN routing turns out to be friction. But L2 adjacency is
not a rule you can narrow: it would put an unpatched consumer Android next to
all seven nodes and `192.168.4.36` — NFS exports, the iSCSI portal, SMB, the
kubelets, etcd on the three control planes, and Headlamp on `:30100`, which
answers on every node IP and whose token `infra/headlamp/README.md` describes
as a full cluster credential. If this cut is ever taken, turn that NodePort off.

The traffic is also one-directional in intent: the cluster initiates, the phone
initiates exactly one thing back. On its own VLAN that is two rules saying
precisely that. On a shared VLAN it cannot be expressed at all.

### The inbound path needs fixing first

Received SMS is delivered to `SMS_RELAY_PUBLIC_URL`, currently
`https://sms-relay.zachd.duckdns.org/api/v1/inbound`. `infra/duckdns` is in
`mode: tailnet`, so that name resolves to a `100.x` node address — **inbound
already depends on Tailscale, which is unreliable on this handset.** The relay
logs show no inbound webhook traffic in recent history, consistent with that
path not working today.

Do not carry the dependency across the cutover. Mirror the `jellyfin-lan`
pattern: a LAN-only NodePort in front of the existing `sms-relay-webhook`
service, with the Android gateway app pointed at
`http://<node-ip>:3XXXX/api/v1/inbound`. This works because —

- the webhook already has its own Ingress specifically to escape Authelia
  forward-auth, so there is nothing to authenticate past;
- it is already HMAC-signed with `secrets.webhookSecret`, which is the real
  authentication, so plaintext across one firewall-restricted hop is
  acceptable;
- an IP has no cert and the app almost certainly will not let you override the
  `Host` header, so the Ingress path cannot be reused as-is;
- no Tailscale, no DuckDNS, no DNS at all.

Pin it to one stable node IP — svclb answers on all seven, and the app holds a
single URL.

## Firewall matrix

| From → To | Policy |
|---|---|
| cluster → trusted | **deny** — the point of the exercise |
| cluster → untrusted | `192.168.30.24:8080` only |
| cluster → internet | allow (via `infra/egress-proxy` where opted in) |
| trusted → cluster | 443, 30096 (Jellyfin), 30100 (Headlamp), 25565 (Minecraft) |
| trusted → NAS | L2 on VLAN 20, no rule |
| untrusted → cluster | `<node-ip>:3XXXX` only |
| untrusted → trusted | deny |
| untrusted → NAS | deny |
| untrusted → internet | allow |
| any → gateway admin | trusted only |

Mirror every rule in IPv6. A v4-only ruleset on a dual-stack network is a
wide-open bypass, and every host here holds a routable GUA.

## The tailnet bypass

All seven nodes, the NAS, and the personal machines are one flat tailnet with
`/32` routes and no ACL restrictions. **A compromised pod that escapes to a node
reaches the laptops over `100.x` regardless of any VLAN boundary.**

Tighten the Tailscale grants so cluster nodes cannot *initiate* to personal
devices, and disable Tailscale SSH acceptance on nodes not administered that
way. Without this the VLAN work is largely cosmetic — this is not an optional
follow-up.

## What changes in this repo

| File | Change |
|---|---|
| `infra/sms-relay/values.local.yaml:14` | `gatewayUrl` → `http://192.168.30.24:8080`. Currently stale at `100.97.132.75`; the live pod already runs `192.168.4.24`. **Publish through the relay bundle or a pod restart reverts it.** |
| `infra/sms-relay/` | New LAN NodePort service for the webhook path. |
| `infra/democratic-csi/values-nfs.yaml:64`, `values-iscsi.yaml:74` | No change — VLAN 1 keeps `192.168.4.36`. |
| `media/jellyfin/values.yaml:13`, `infra/k8up/values.yaml:37` | No change, same reason. |

## Cutover order

1. **Confirm the open questions below.** Buying decisions depend on them.
2. Managed switch in, unmanaged switch out, everything still untagged on VLAN
   1. No behaviour change; verify the cluster is healthy before going further.
3. New gateway replaces eero routing; eeros to bridge mode on VLAN 20. VLAN 1
   keeps its addressing.
4. NAS trunk port, tagged VLAN 20 interface, interface bindings.
5. Move personal devices to VLAN 20. Verify SMB against `192.168.20.36` and
   the `trusted → cluster` allowances (Jellyfin on the TVs is the one people
   notice).
6. sms-relay webhook NodePort deployed and verified over the LAN path, *before*
   the handset moves.
7. Handset to wired VLAN 30, DHCP reservation `192.168.30.24`, update
   `gatewayUrl`, re-point the app's webhook URL. Send and receive a real
   message.
8. Tailscale ACLs.
9. Drop `cluster → trusted` to deny. Last, because it is the step that reveals
   any dependency nobody wrote down.

## Open questions

- **Sonic WAN handoff.** Residential fiber is usually plain DHCP, but older
  Fusion/DSL wants PPPoE and some handoffs need a WAN VLAN tag. Whatever
  replaces the eero has to match.
- **IPv6 prefix size.** The per-VLAN design wants a `/56` delegation so each
  segment gets its own `/64`. Nodes currently hold `2001:5a8:4a45:6100::/64`;
  the eero will not say what it was actually delegated, and a real router will.
  If it is only a `/64`, run ULA on VLANs 20 and 30 instead.
- **NAS NIC count.** The trunk design needs one NIC and a managed switch port.
  A second physical NIC would also work and is simpler to reason about — worth
  checking what the box has before committing to tagging.
- **mDNS.** Casting and Jellyfin auto-discovery stop crossing segments. If the
  TVs land on VLAN 20 and want to discover Jellyfin on VLAN 1, an mDNS
  reflector between those two is needed.
