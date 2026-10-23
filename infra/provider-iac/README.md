# Provider infrastructure as code

Status 2026-10-22: **implemented and locally validated; not adopted or deployed**.
No DigitalOcean, Cloudflare account API, or Tailscale policy API credential was
found in the accessible homelab/Personal vault item inventories. Tunnel connector
tokens, Tailscale device sign-in and mounted 1Password credentials do not grant
these provider permissions. No live provider plan or inventory is claimed.

Start with [the completion checklist](../../plans/provider-iac-adoption.md) for the owner's one-time
credential setup and the agent's inventory, deployment and verification steps.

The recovery rehearsal is deferred at the owner's request. Provider adoption
can proceed independently once credentials are available.

## Ownership

Three independent OpenTofu roots avoid coupling DNS, firewall and tailnet changes:

| Root | Scope | First change |
|---|---|---|
| `digitalocean` | Existing Whatnow/egress droplets, existing backup schedules, one dedicated cloud firewall per droplet | Import exact live properties; restrict public ingress |
| `cloudflare` | Explicitly selected existing A/AAAA/CNAME records | Import with no changes; later review DNS changes separately |
| `tailscale` | The complete existing tailnet policy | Import unchanged; then narrow egress access with policy tests |

Host packages, services and runtime secrets remain owned by
[Whatnow Ansible](../whatnow-droplet/README.md) and
[egress Ansible](../egress-droplet/README.md). No application `.env`, user-data,
Tailscale enrollment key or tunnel token is put into this provider configuration.
DuckDNS remains owned by its updater. Cloudflare zones, email/TXT records, tunnel
configuration, account settings and unrelated provider resources are not adopted.

OpenTofu is pinned to **1.13.2**. Providers are pinned and their signed dependency
locks are committed. Install the matching OpenTofu release from its official
release artifacts and verify its checksums before running it. Do not replace
OpenTofu with Terraform: the state encryption configuration is OpenTofu-specific.

## Credentials and state

The default vault references below are a **setup contract**, not a claim that
these items have been created:

| Reference | Contents/access |
|---|---|
| `op://homelab/provider-iac-digitalocean/token` | DO custom-scoped API token: read droplets, backup policies, images, sizes, regions, VPCs, tags, volumes and firewalls; update droplet backup policies and create/update firewalls for apply. No delete permission is needed. Verify the provider's required action scopes against an actual plan. |
| `op://homelab/provider-iac-cloudflare/token` | Cloudflare API token with Zone Read and DNS Read/Edit restricted to selected zones |
| `op://homelab/provider-iac-tailscale/token` | Tailscale API key for the intended tailnet; this initial wrapper uses API-key auth. A scoped policy OAuth client is a follow-up credential improvement; do not use a device enrollment key. |
| `op://homelab/provider-iac-state/passphrase` | Random secret of at least 32 characters, generated once and retained for state recovery |

Existing item references can be passed with `--credential-ref` and
`--state-key-ref`; never pass raw tokens. `provider.sh` obtains the existing
1Password session. `run.py` sends provider tokens only through their provider
environment variables, and the state key through `TF_ENCRYPTION`. Shell tracing,
OpenTofu debug logging and inherited CLI argument overrides are disabled by the
wrapper. Root/the same OS user can still inspect process environments.

State and saved plans use mandatory PBKDF2/AES-GCM encryption. No plaintext
fallback is configured. Encrypted state, backups and plugin data persist under
`~/.local/state/selfhosted-provider-iac/<stack>/` with private permissions, outside
Git. The state key stays in 1Password. Do not rotate/delete it without following
OpenTofu's encryption migration procedure. Losing it makes state unreadable.

This is a **single-workspace local backend**, with a per-stack runner lock and
OpenTofu state locks. Do not run a second checkout/machine against an independent
state copy. Back up the encrypted state directory to the existing private backup
storage after adoption/changes; a state key alone is not a state backup. Before
moving operators, stop writers, copy the encrypted state and restore with the same
key. A shared locking backend and automated state backup are not provisioned here.
No paid/offsite cluster storage is introduced.

## Read-only discovery and adoption

`discover.py` only issues GET requests. It refuses redirects, ambiguous production
identities and incomplete pagination. Output is a new private directory, never
overwritten. Do not print raw provider responses or copy unreviewed tailnet policy
comments into Git; policy files can contain sensitive operator information.

```sh
# Substitute VERIFIED operator public CIDRs (not Tailscale's 100.x addresses).
infra/provider-iac/provider.sh discover digitalocean \
  --ssh-cidr OPERATOR_PUBLIC_IP/32 --output infra/provider-iac/do.local

# Select actual existing names. Repeat --name for each intended record.
infra/provider-iac/provider.sh discover cloudflare --zone whatnow.gg \
  --name whatnow.gg --output infra/provider-iac/cf-whatnow.local

infra/provider-iac/provider.sh discover tailscale \
  --output infra/provider-iac/tailnet.local
```

Discovery preserves the live droplet sizes, VPCs, volume attachments, tags and
backup plan/day/hour. It refuses disabled backups or unsupported schedules rather
than silently introducing cost or changing retention. Whatnow's live ID is checked
against 587384488; both droplets are matched to their recorded public addresses.
It inspects **all** cloud firewall associations, including tags. Shared, overlapping
or unsettled firewalls require explicit ownership reconciliation before proceeding;
adding a restrictive firewall alongside a permissive one is not a fix.

Review the generated inputs against the provider console. Move reviewed nonsecret
inventory into a tracked `*.json` file (not `*.tfvars.json`, which is ignored) and
commit it before apply. For multiple Cloudflare zones, merge the selected `records`
maps into **one** file: passing only one zone's subset to an already-adopted state
would propose deleting the other records and be rejected. When moving the reviewed
tailnet policy, update `policy_path` to its new absolute path for each checkout.
Do not commit credentials, state, plans, or unreviewed provider inventory.

```sh
# DNS and tailnet adoption must have ZERO resource changes, only imports.
infra/provider-iac/provider.sh cloudflare adopt --vars /absolute/path/dns.json
infra/provider-iac/provider.sh tailscale adopt --vars /absolute/path/tailnet.json

# DO adoption includes firewall hardening. Review this diff before applying.
infra/provider-iac/provider.sh digitalocean plan --vars /absolute/path/digitalocean.json
infra/provider-iac/provider.sh digitalocean apply
```

`adopt` applies only a no-change import plan. `plan` creates an encrypted saved
plan; `apply` rechecks its guardrails and applies that exact plan within 15 minutes.
Deletion, replacement, new droplets, new DNS records and fresh ACL resources are
rejected. The only allowed creates are the two named managed firewalls. Droplet
updates are limited to backups and the non-destructive resize preference; size,
network and volume changes are rejected. `prevent_destroy` is a second layer,
not a substitute for the plan guard. There is no destroy/target/replace shortcut.
Only operate through this wrapper for production.

## Firewall behavior and verification

- Whatnow: Cloudflare ranges from the existing host config may reach TCP 80/443;
  reviewed operator CIDRs may reach SSH. UDP 443 is closed.
- Egress: TCP 80/443/25565 and UDP 3478/49152–49500 remain public. SSH is limited
  to operator CIDRs. Squid 3129 has **no public allow rule**. The Tailscale transport
  remains outbound/DERP-capable; this does not add a public UDP 41641 opening that
  the current host firewall does not permit.
- Both: ICMP is allowed; TCP/UDP egress is preserved for updates, DNS, Tailscale,
  Squid and TURN. Private/metadata denial remains enforced by the host's service
  rules. This is ingress hardening, not a claim of provider-enforced egress isolation.

Before apply, verify SSH from the proposed operator address and retain console
access. An existing connection is not evidence a new connection will work. After
apply, check DO firewall status/pending changes, re-enumerate every ID/tag-based
association, test new SSH connections, Whatnow health through Cloudflare and failed
direct-origin access. Verify Jellyfin health, authenticated proxy/unauthenticated
rejection and TURN allocation using the existing host checks. **Never probe
Minecraft TCP 25565**; it wakes the paused production JVM. Re-plan must converge.
Rollback changes the desired rules and applies a fresh reviewed plan; do not
remove the firewall, disable backups or flush host nftables.

## Tailnet hardening after no-change import

The provider owns the **whole** policy, not a mergeable grants fragment. Preserve
SSH rules/check mode, groups, tag owners, autoApprovers, nodeAttrs and other
applications. The reviewed target for egress `100.121.204.109` is only the
`media_nodes` on TCP 30096 and `minecraft_nodes` on TCP 25565 from
`../egress-droplet/config.yml`. Other tailnet destinations/ports should be denied.

A narrow new grant does not override broader existing grants or ACLs. Inspect all
matching wildcard, user/group, autogroup and tag sources; remove the egress device
from broad access while preserving access for other devices. Decide its tag/owner
migration only after inspecting the full policy and live device ownership. Add
policy tests for allowed backends, denied SSH/API/other peers and preserved operator
access. The provider validates policy/tests during planning. Do not replace the
whole policy with an invented restrictive example. No live grant change or tag
migration has been performed yet.

## Validation and pending deployment

```sh
python3 -m unittest discover -s infra/provider-iac/tests -v
# Optional local encryption and mocked firewall plan tests (no cloud API calls):
TOFU_TEST_BIN=/absolute/path/tofu python3 -m unittest discover -s infra/provider-iac/tests -v
python3 infra/provider-iac/run.py digitalocean validate
python3 infra/provider-iac/run.py cloudflare validate
python3 infra/provider-iac/run.py tailscale validate
```

All three pinned provider schemas validate. Safety tests cover destructive plans,
accidental resource creates, volume/size changes, backup disabling, ambiguous host
identity, shared firewall associations and selective DNS inventory. The encryption
integration test verifies real encrypted plans/state, successful decryption, wrong
key rejection and failure without encryption configuration. The mock firewall
plan evaluates Cloudflare-only web ingress, operator-only SSH, no public Squid,
enabled backups and disabled disk growth. OpenTofu 1.13.2's test runner crashes
on mocked import blocks, so that test uses a disposable copy without imports;
the production imports remain intact. These checks do not
prove live API permissions, imported defaults, connectivity or policy semantics.

Pending: credentials/state key, live inventory review, committed desired inputs,
imports/plans, apply, connectivity/drift checks, full-policy egress hardening, and
encrypted state backup. Provider IaC stays open until those live steps pass.

References: [DO droplets/backups](https://registry.terraform.io/providers/digitalocean/digitalocean/2.106.0/docs/resources/droplet),
[DO firewalls](https://registry.terraform.io/providers/digitalocean/digitalocean/2.106.0/docs/resources/firewall),
[Cloudflare DNS](https://registry.terraform.io/providers/cloudflare/cloudflare/5.28.1/docs/resources/dns_record),
[Tailscale policy ownership](https://registry.terraform.io/providers/tailscale/tailscale/0.29.2/docs/resources/acl),
[OpenTofu encryption](https://opentofu.org/docs/language/state/encryption/).
