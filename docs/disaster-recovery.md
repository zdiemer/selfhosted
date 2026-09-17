# Disaster recovery assessment

A snapshot, not a runbook: how many nodes the cluster can lose at once, where
the single points of failure are, and whether it could be rebuilt from zero with
this repo and 1Password. Assessed 2026-09-17, just after zachd-ubuntu-6 was
removed, from the live cluster (read-only kubectl) and this repo. Node names,
memory figures and pins will drift; re-check before relying on any one line.

## Summary

- **Nodes you can lose at once:** worst case 1, best case about 4.
  - etcd has 3 members (zachd-ubuntu, -3, -5), so it survives losing 1 of them.
  - zachd-ubuntu-2 or zachd-ubuntu-4 alone already takes a service down.
  - All three laptops plus zachd-ubuntu-5 can die together and every movable
    pod still fits somewhere else.
- **Biggest single point of failure:** the TrueNAS at 192.168.4.36. It holds
  about 50 PVs, the registry, every k8up restic repository and the weekly
  secrets archive. Nothing is copied offsite.
- **Rebuild from zero:** possible if the NAS survives, but slow and mostly by
  hand. There is no scripted way to create the first server and no install
  order. If the NAS is lost too, all app data and all backups go with it; only
  the chart secrets in 1Password remain.

## Control plane

- 3 servers, embedded etcd. No sign of a leftover ubuntu-6 member, though the
  member list itself needs `etcdctl` on a server to confirm.
- **No VIP or load balancer in front of the API.** Most agents' `server:`
  points at zachd-ubuntu's tailscale address; zachd-ubuntu-4 points at
  zachd-ubuntu-3. Agents that are already running track all three servers, so
  this only bites a fresh join or an agent restart while that server is down.
- etcd snapshots are the k3s defaults on each server's local disk. No offsite or
  S3 target is set anywhere.

## Single points of failure

Ranked by impact.

| # | What | Why it is a single point of failure |
|---|---|---|
| 1 | TrueNAS 192.168.4.36 | `truenas-iscsi`, `truenas-nfs` and SMB PVs; the registry; all 16 restic repos; the secrets backup. No offsite copy ([`infra/k8up/`](../infra/k8up/) says offsite is "not built"). |
| 2 | Authelia on zachd-ubuntu-2 | Pinned by hostname with `authelia-data` on local-path. When the node dies, every forward-auth login fails. |
| 3 | talaria Postgres on zachd-ubuntu-3 | 200Gi local-path volume, pinned by hostname. Down until the node comes back. |
| 4 | buildkit on zachd-ubuntu-4 | Pinned, with its cache on local-path. Without it no image can be built, including the rebuild after a registry loss. |
| 5 | VAAPI on zachd-ubuntu-1 | Now the only node labelled `media.zachd/vaapi=true`, so Jellyfin transcoding stops with it. The win11 VM and talaria Elasticsearch (local-path) are stuck there too. |
| 6 | API server address | Agents point at one server (see Control plane). |
| 7 | Single-replica infra | coredns, metrics-server, local-path-provisioner, csi-smb and democratic-csi controllers, snapshot-controller, registry, authelia and its redis, crowdsec-lapi, k8up, keda, the arc controller, kubevirt. None has a PDB. After a node dies they are down about 5 minutes, the default eviction timeout. |
| 8 | claude-bridge on zachd-ubuntu | `hostPath: /home/zachd/.claude`. |

These are already redundant:
- Traefik runs 3 replicas (PDB min 2), and its klipper LB runs on every node.
- cloudflared runs 2 replicas (PDB min 1).
- DuckDNS repoints to the next Ready node within about 5 minutes.
- None of the three laptops carries anything unique.

## Capacity

Memory requests versus allocatable, in Gi:

| Node | Allocatable | Requested | Free |
|---|---|---|---|
| zachd-ubuntu (control plane, no workloads) | 14.8 | 2.2 | 12.5 |
| zachd-ubuntu-1 | 36.9 | 17.6 | 19.3 (only 0.4 CPU free) |
| zachd-ubuntu-2 | 28.9 | 21.9 | 7.0 |
| zachd-ubuntu-3 | 28.9 | 23.3 | 5.6 |
| zachd-ubuntu-4 | 28.8 | 25.5 | 3.3 |
| zachd-ubuntu-5 | 26.8 | 21.4 | 5.4 |
| laptop / laptop-2 / laptop-6 | ~6.3 each | 2.6 / 2.4 / 0.8 | 3.8 / 4.0 / 5.4 |

The large pods are talaria-postgres (20Gi), Minecraft (13.5Gi), ollama (10Gi),
the win11 VM (8.4Gi) and buildkit (8Gi). Only zachd-ubuntu-1 has 10Gi free, and
it is out of CPU, so Minecraft and ollama cannot reschedule anywhere.

To test simultaneous losses, displaced pods were packed into the free capacity
that remained, respecting pins and CPU:

| Nodes lost | Combinations where every movable pod fits | Worst combination |
|---|---|---|
| 1 | all except zachd-ubuntu-2 (Minecraft) and zachd-ubuntu-4 (ollama) | |
| 2 | 21 of 36 | zachd-ubuntu-2 + -4 (23.5Gi stranded) |
| 3 | 34 of 84 | zachd-ubuntu-2 + -4 + -5 (39 pods, 31Gi stranded) |
| 4 | 22 of 126 | |

## If this node dies

| Node | Impact |
|---|---|
| zachd-ubuntu | etcd 2/3. coredns moves after about 5 min. Fresh joins and agent restarts that point here fail. claude-bridge is lost. |
| zachd-ubuntu-1 | Jellyfin loses hardware transcoding. The win11 VM and talaria Elasticsearch are stuck. DuckDNS moves to zachd-ubuntu-2. |
| zachd-ubuntu-2 | Authelia is down, so all forward-auth logins fail. Minecraft cannot reschedule. |
| zachd-ubuntu-3 | etcd 2/3. talaria Postgres and the builder-scraper are down until the node returns. |
| zachd-ubuntu-4 | buildkit is down and its cache is lost. ollama cannot reschedule. |
| zachd-ubuntu-5 | etcd 2/3. Everything fits elsewhere; the idle vmlab disks become unreachable. |
| any laptop | No real impact. |
| TrueNAS | Most stateful apps, the registry and every backup. |

## Bootstrapping from zero

What works:
- Nearly every chart that needs secrets has a `values.local.tpl.yaml` backed by
  1Password. talaria's sops age key and the k8up restic password are in
  1Password too.
- Every first-party image can be rebuilt from its submodule with `build.sh`,
  except where noted under "Not in git or backups" below.

Gaps, worst first:

1. **Nothing creates the first server.** `k3s-cluster/join-cluster.sh` only
   joins an existing cluster: it reads the node token over SSH from a live
   server and copies its k3s config. Neither the first server's `config.yaml`
   (disable flags, CIDRs, `secrets-encryption`) nor the encryption key is in git
   or has a documented backup.
2. **No install order.** Namespaces are created by hand, and only
   [`infra/snapshot-controller/`](../infra/snapshot-controller/) says "install
   FIRST". The working order is:
   1. NAS datasets and API key (manual, see
      [`infra/democratic-csi/`](../infra/democratic-csi/))
   2. snapshot-controller
   3. democratic-csi
   4. priority-classes
   5. duckdns
   6. traefik-certs
   7. traefik
   8. registry (writes `registries.yaml` on every node)
   9. buildkit
   10. rebuild and push first-party images
   11. apps
3. **The registry sits at the end of a dependency chain.** It needs NAS storage,
   and pushes go through Traefik, which needs the wildcard cert. buildkit also
   needs a userns sysctl that `join-cluster.sh` does not set.
4. **Manual steps nobody scripted:**
   - the VAAPI node label and render GID 992
   - the restic NFS export on TrueNAS
   - the Cloudflare tunnel's hostname routes
   - the egress VPS
   - the buildkit GHCR PAT
5. **Not in git or backups:**
   - talaria frontend fonts
   - SMT IMAGINE client data (its PVC is `k8up.io/backup: "false"`)
   - the FFXIV 1.x client
   - Jellyfin Intro Skipper (manual install; 12.0 also resets VAAPI)
   - claude-bridge's `.claude`
   - Deliberately unbacked: the ROM library, smitele matchdata, bitmagnet
     Postgres and the cloud-game library.
6. **Backups are weekly.** A restore can lose up to a week. talaria Postgres now
   has a pre-backup dump pod
   ([`infra/k8up/templates/prebackuppods.yaml`](../infra/k8up/templates/prebackuppods.yaml)),
   but nobody has checked that it produces a restorable dump, and the k8up
   README TODO still says "no backup at all".

Also out of date: the egress-proxy relay lists still name
`zachd-ubuntu-laptop-1` and omit zachd-ubuntu-3, -4 and -5.

## Recommended next steps

In order of risk removed per unit of effort.

1. **Get backups off the NAS.** Add an offsite restic target (B2 or similar)
   for k8up and the weekly secrets archive. Set `etcd-s3` on the servers so
   snapshots leave the box.
2. **Unpin Authelia.** Move `authelia-data` to `truenas-iscsi` and drop the
   zachd-ubuntu-2 hostname pin. The local-path choice was made so a NAS outage
   cannot block logins; that is a smaller risk than one node blocking them.
3. **Back up the k3s bootstrap material in 1Password:** the server
   `config.yaml`, the node token and `encryption-config.json`.
4. **Write the first-server path.** Add a `--cluster-init` mode to
   `join-cluster.sh` and a `docs/bootstrap.md` holding the install order above.
5. **Make node setup complete.** `join-cluster.sh` should set the buildkit
   userns sysctl, the render GID and optional labels (`media.zachd/vaapi`).
   Label a second VAAPI node.
6. **Free headroom for the big pods.** Right-size the Minecraft and ollama
   memory requests, or add RAM, so both can reschedule after a single node loss.
7. **Put a stable address in front of the API.** Use kube-vip or a DNS name
   over the three servers, and repoint every agent's `server:` at it.
8. **Harden the Postgres path.** Verify that the talaria dump restores. Then
   either move Postgres to NAS storage or add streaming replication so
   zachd-ubuntu-3 is not a hard dependency.
9. **Add PDBs and a second replica** for coredns, and the registry once it is
   on shared storage.
10. **Housekeeping.**
    - Fix the egress relay node lists.
    - Delete the orphaned local-path PVs: the old traefik and talaria-postgres
      volumes on zachd-ubuntu-1 and two crowdsec volumes on the laptop.
    - Confirm the router's 80/443/25565 port forwards are gone.
    - Update the k8up README TODO.
11. **Practise.** Restore one k8up backup into a scratch namespace, and one etcd
    snapshot into a throwaway VM (`dev/vmlab`), before either is needed for real.
