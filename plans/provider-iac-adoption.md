# Finish provider IaC adoption

As of 2026-10-22, the code is committed and locally validated, but no live
resources have been imported or changed. The recovery rehearsal is deferred.
This checklist resumes the work without requiring that rehearsal first.

## 1. Owner: provide provider access once

Create the following items in the existing **homelab** 1Password vault, or supply
references to equivalent existing fields. Send the item references to the agent;
do not paste tokens into chat. Each provider can be completed independently.

| Item / field | How to obtain it |
|---|---|
| `provider-iac-digitalocean` / `token` | DigitalOcean control panel → API → Tokens → generate a custom-scoped token. Allow discovery reads for droplets, backup policies, images, regions, sizes, VPCs, volumes, tags and firewalls, plus backup-policy updates and firewall create/update for deployment. No droplet creation or deletion is needed. The exact required action scopes must be checked against the provider's live requests; do not solve a missing read permission by granting unrestricted account access. |
| `provider-iac-cloudflare` / `token` | Cloudflare profile → API Tokens → create a custom token. Give Zone Read and DNS Edit for only the zones being adopted, initially `whatnow.gg`; include `diemer.codes` when adopting its selected records. DNS Edit permits the DNS reads used for discovery. |
| `provider-iac-tailscale` / `token` | Tailscale admin console → Settings → Keys → generate an API access token for the intended tailnet. The current wrapper accepts an API key, not a device enrollment/auth key or an OAuth client secret. Record expiry and revoke/replace when no longer needed. Scoped OAuth requires a wrapper extension before use. |

The agent can create `provider-iac-state` / `passphrase` in 1Password using its
existing vault access: generate a random value of at least 32 characters, verify
its vault round trip, and never replace it if encrypted state already exists.
There is no need for the owner to generate this secret manually.

For DigitalOcean, also identify a public operator SSH source CIDR and verify it
can open a new SSH connection to both droplets. A home address can change; use
an independently verified stable operator path or plan to update the allowlist
before it changes. Tailscale's `100.x` addresses are not public firewall sources.
Retain provider console access before changing SSH ingress.

## 2. Agent: prepare the existing workspace

Use OpenTofu **1.13.2**, the committed provider locks, and the scripts in
[`infra/provider-iac/`](../infra/provider-iac/). Run commands from the repository root.
Do not create a second independent state for the same resources. Check for
existing encrypted state under `~/.local/state/selfhosted-provider-iac/` before
initializing a new operator workspace. Keep keys, state and plans outside Git.

```sh
python3 -m unittest discover -s infra/provider-iac/tests -v
python3 infra/provider-iac/run.py digitalocean validate
python3 infra/provider-iac/run.py cloudflare validate
python3 infra/provider-iac/run.py tailscale validate
```

The optional integration tests require `TOFU_TEST_BIN=/absolute/path/tofu`.
They exercise encrypted local state and mocked firewall plans, not live APIs.

## 3. Agent: inventory, review and commit desired inputs

```sh
# Replace the placeholder with the verified operator CIDR.
infra/provider-iac/provider.sh discover digitalocean \
  --ssh-cidr OPERATOR_PUBLIC_IP/32 --output infra/provider-iac/do.local

infra/provider-iac/provider.sh discover cloudflare --zone whatnow.gg \
  --name whatnow.gg --name www.whatnow.gg --output infra/provider-iac/cf.local

infra/provider-iac/provider.sh discover tailscale \
  --output infra/provider-iac/tailnet.local
```

Use `--credential-ref op://VAULT/ITEM/FIELD` for non-default references. Discovery
is read-only and refuses to overwrite its output directory. If a requested DNS
name does not exist, inspect the actual zone and deliberately adjust the selection;
do not invent a replacement record. For other zones, discover separately, select
only the intended records, and merge their `records` maps into one desired file.

Review these facts before promoting the generated files into tracked nonsecret
JSON under `infra/provider-iac/` (for example `digitalocean/production.json` and
`cloudflare/production.json`):

- Droplet IDs/public IPs, current sizes, VPCs, tags and volume attachments match
  production. Backups are actually enabled, with the current plan/day/hour retained.
- Every applicable firewall is accounted for, including tag associations. The
  current discovery refuses shared/multiple firewalls; resolve their ownership
  explicitly instead of attaching another firewall and assuming it overrides them.
- DNS proxy flags, targets and TTLs are preserved. Mail/TXT and unrelated records
  stay outside scope. Keep all adopted DNS records in the combined desired map.
- Export and review the **whole** tailnet policy. Preserve SSH checks, groups,
  tags, node attributes and unrelated service access. Check comments for secrets
  before committing the policy. Point the input's `policy_path` to its actual
  absolute path in this checkout; generated local path files need not be committed.

Commit the reviewed desired configuration before any apply. The generic stack
code alone does not constitute adoption of actual account resources.

## 4. Agent: import and apply in separate steps

```sh
# No resource changes are permitted in these initial imports.
infra/provider-iac/provider.sh cloudflare adopt \
  --vars "$PWD/infra/provider-iac/cloudflare/production.json"
infra/provider-iac/provider.sh tailscale adopt \
  --vars "$PWD/infra/provider-iac/tailnet.local/inventory.tfvars.json"

# DO includes the reviewed firewall changes as well as importing the droplets.
infra/provider-iac/provider.sh digitalocean plan \
  --vars "$PWD/infra/provider-iac/digitalocean/production.json"
infra/provider-iac/provider.sh digitalocean apply
```

Inspect the complete plan before applying. Saved plans expire after 15 minutes;
regenerate if necessary. If a guard rejects resizing, deletion, replacement or
volume changes, fix the inventory/configuration mismatch. Do not bypass the guard
with raw OpenTofu, target flags or a fresh state. Provider authentication failure
means fix the credential/scope, not that a resource should be recreated.

After unchanged tailnet import, narrow the egress device's effective permissions
to the backend nodes/ports documented in [the README](../infra/provider-iac/README.md#tailnet-hardening-after-no-change-import).
Inspect every broader matching rule: adding a narrow grant does not subtract
permissions. Add allowed/denied policy tests, preserve operator access, then use
the same `plan --vars ...` / `apply` workflow for the Tailscale stack. Do not
change device ownership/tags without accounting for the effect on its access.

## 5. Agent: verify and record completion

- Confirm firewall status is succeeded with no pending changes, then re-enumerate
  associations so no additional firewall permits unwanted traffic.
- Test new SSH sessions, Whatnow's proxied health and blocked direct-origin HTTPS,
  Jellyfin health, authenticated proxy success/unauthenticated rejection, and the
  existing TURN allocation test. Never probe Minecraft TCP 25565.
- Verify the egress device's intended backend access and denial to unrelated
  peers/SSH/Kubernetes API, plus preserved administrator access. Policy tests are
  necessary but do not replace relevant runtime checks.
- Re-plan each adopted stack and obtain no changes. Confirm backup schedules in
  the provider API; this verifies configuration, not recoverability.
- Back up encrypted state to existing private backup storage. Retrieve the copy
  into an isolated location and verify decryption with the vault key without
  applying it. Do not print decrypted state. Store the key independently in the vault.
- Record resource IDs, commit/revision, test results, state-backup location and
  remaining exceptions. Update the shared hardening checklist and commit/push.

Do not mark provider IaC complete until live adoption, hardening, verification and
state backup pass. Rehearsal remains deferred, and paid offsite cluster storage
remains outside the accepted scope.

## Stronger origin identity can proceed separately

See [the Whatnow origin identity plan](whatnow-origin-identity.md).
Only Cloudflare access is missing for that work; DigitalOcean/Tailscale tokens
and provider adoption are not prerequisites. For the proposed zone-specific
client certificate, also grant **SSL and Certificates Edit** on `whatnow.gg`
(the API names this permission **SSL and Certificates Write**). The DNS-only
provider token above does not grant that permission. A separate narrowly scoped
Cloudflare token is also suitable.
