# Egress SFO3 managed configuration

Adopted from the live Ubuntu 24.04 droplet `homelab-sfo3` at `147.182.206.23`
(Tailscale `100.121.204.109`). `config.yml` records the reviewed relay topology.
Production credentials were migrated without rotation into 1Password's
`homelab/egress-sfo3-production` item, field `secrets.json`.

```sh
infra/egress-droplet/apply.sh --check --diff
infra/egress-droplet/apply.sh
# Explicit disruptive maintenance, followed by automatic recovery checks:
infra/egress-droplet/apply.sh --reboot
```

Edit non-secret configuration in Git and credentials in the vault, then apply.
Do not edit runtime files over SSH. Tailscale SSH may require an identity check;
complete that check rather than bypassing it. Package installation/upgrades are
separate maintenance: the playbook checks installed packages with `dpkg-query`
without loading the large APT cache on this 512 MiB host.

This follows the Whatnow model: Ansible desired state in Git, credentials in
1Password, tmpfs staging, hidden secret diffs, validated configurations and
runtime probes. It adopts an existing egress host with its package repositories,
CrowdSec setup/collections, ACME state and Tailscale enrollment intact. It is not
yet a bare-droplet provisioning recipe or a Tailscale account policy manager.

## Adoption and recovery

The original adoption compared listeners, service users, package versions, DNS,
Caddy/HAProxy backends, TURN settings and firewall rules with the live host before
setting `live_configuration_reviewed: true`. The import helper reads current
Squid/TURN credentials, legacy Squid TLS files and CrowdSec credentials directly
into memory, preserves their ownership/modes, and verifies the vault round trip.
It refuses to overwrite an existing item. Keep those credentials for recovery;
changing the proxy or TURN password also requires updating cluster clients.

For a replacement host, restore the service/package repositories and relevant
state first, re-enroll Tailscale, review addresses and UIDs, then apply. CrowdSec
collections, ACME state and Tailscale identity remain host-managed. This is an
adoption playbook, not a bare-droplet bootstrap. Minecraft relay workers run as
`haproxy`; the master retains its normal service-management privileges.

## Enforced policy

- Squid denies private, loopback, link-local, tailnet, multicast and the droplet's
  own public address before the authenticated allow rule. Public HTTP/CONNECT
  and existing Basic authentication remain available over Tailscale.
- Kernel rules independently deny private/local destinations for the service
  users, including metadata, so a Squid policy bypass does not suffice. Incoming
  connection replies are allowed first, preserving authenticated proxy replies
  and SSH. Local DNS and Caddy's localhost reload API are explicit exceptions.
- Only Caddy may open tailnet connections to configured media nodes on 30096;
  only HAProxy workers may reach configured Minecraft nodes on 25565. Arbitrary
  peers on those ports are no longer allowed. ICMP diagnostics remain available.
- Public listener ports and existing connection meters are preserved. The proxy
  has no public fallback. Unused DDNS proxy-allowlist refresh is retired.
- CrowdSec enforcement hooks are checked directly, not inferred from daemon
  status. Missing hooks trigger bouncer recovery. Its startup is ordered after
  nftables without tying its lifetime to firewall restarts.
- Firewall updates replace only `inet egress` atomically and preserve CrowdSec and
  Tailscale tables. The packaged nftables unit's potentially global-flush stop
  action is overridden. Public service units require the firewall before start.
- Runtime tests check proxy authentication, successful public CONNECT, private
  destination rejection, direct kernel blocking as the Squid user, and media
  reachability as Caddy. No response bodies or credentials are printed.

A root compromise can still change this host firewall. Independently enforced
Tailscale grants/ACLs must also restrict the droplet to the exact backend nodes
and ports. Tailnet policy API access has not been established, and those independent
grants/ACLs have not been changed. DNS/DO resources, backup policy and provider DDoS
protection remain separate, as with the Whatnow host configuration.

Existing bootstrap remains available for an unmanaged host, but refuses to run
once `/etc/egress-iac-managed` is present. Do not use it to change an adopted host.
Secrets are delivered as protected runtime files; host root and whole-disk backups
can still read them. For replacement-host recovery, preserve the vault item and
restore/re-enroll the relevant service state; do not clone a Tailscale identity
onto two running machines.

## Verification

Every apply checks authenticated public CONNECT, unauthenticated rejection,
metadata/private/own-address denial, direct kernel restrictions under the Squid
worker UID, media reachability as Caddy, all service states, and CrowdSec input
hooks for both address families. Probes never print credentials or response
bodies. Do not add recurring Minecraft TCP health checks; they wake its paused JVM.

With an authenticated `op` session, `python3 infra/egress-droplet/verify-turn.py`
checks the public UDP path: authentication challenge, authenticated allocation,
loopback-peer denial and explicit release. It sends no relayed traffic. The
controller must have outbound UDP access for this optional test.

Maintenance testing also inserts a short-lived decision for a reserved test IP,
verifies it reaches nftables and survives a firewall-service restart, then deletes
it. A successful reboot must pass runtime isolation checks BEFORE the playbook
reconciles anything, so reconciliation cannot hide a failed boot configuration.


## Replacement provisioning and identity recovery

`prepare.sh` provisions the supported Ubuntu 24.04 amd64 base: reviewed signing
keys/repositories, missing service packages, directories, CrowdSec collections,
and Tailscale. It checks installed packages with `dpkg-query`, without loading
Python APT's package cache on an already-provisioned small host. Small hosts get
1 GiB swap before package operations, provided at least 2 GiB disk is free.
A fresh host keeps a persistent SSH/Tailscale-only bootstrap firewall while
service packages are installed with automatic service startup suppressed.

With an authenticated 1Password session:

```sh
python3 infra/egress-droplet/recovery-state.py backup --document egress-sfo3-recovery-YYYYMMDD
python3 infra/egress-droplet/recovery-state.py verify --document egress-sfo3-recovery-YYYYMMDD
```

The encrypted document includes a consistent SQLite backup of CrowdSec's local
API database, its configuration/console identity, and Caddy's ACME state. The
October 21 archive was retrieved and byte-verified against the source; it is
5.2 MB. Archives deliberately exclude Tailscale's machine identity. Refresh
archives after identity/registration changes; they do not contain application
data from the separate Whatnow droplet.

For a replacement, fence the old host first. Install the operator's SSH public
key, verify its new SSH host key, point `inventory.yml` at it, then:

1. Run `prepare.sh`. The temporary firewall remains until final verification.
2. Enroll the new machine with `tailscale up` and authorize its intended grants.
3. Restore the chosen vault document with `recovery-state.py restore --host HOST
   --document NAME`. It refuses any already-adopted host.
4. Update `public_ipv4` and review backend addresses in `config.yml`; restore
   existing service credentials from the existing vault item using `apply.sh`.
5. `apply.sh` verifies the enrolled identity, public IP, proxy containment,
   CrowdSec hooks and services before removing only the bootstrap firewall.
6. Update DNS/provider firewall rules and verify the public media and TURN paths.
   Do not add Minecraft probes that wake its JVM.

A clean-host recovery still needs a rehearsal on a replacement droplet. Syntax
checks and live convergence do not establish that the entire recovery works.
DigitalOcean resource creation, provider backups and tailnet grants remain
outside this playbook; credentials for those APIs are not available here.

On October 21, the initial prerequisite run triggered severe memory pressure on
the 458 MiB host with no swap. The kernel killed CrowdSec at 16:24 UTC; SSH and
the media edge temporarily stopped responding. The host recovered without a
reboot, and CrowdSec restarted. The revised prerequisite run completed with
1 GiB swap active and both package-install tasks skipped. Runtime proxy
authentication/containment checks passed, and Jellyfin's public health endpoint
returned HTTP 200. This verifies recovery of the existing host, not replacement
provisioning.

The workspace's mounted `claude-workspace-op-credentials` Secret provides
1Password access; it does not contain a DigitalOcean API token. Provider-level
recovery therefore requires the DigitalOcean console or a separately supplied
vault reference for an appropriately scoped provider credential. Do not paste
provider tokens into chat or commit them to this repository.
