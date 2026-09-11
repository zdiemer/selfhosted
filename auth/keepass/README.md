# KeePass — self-hosted vault, accessible across devices

Helm chart that runs two complementary pods in the `auth` namespace:

- **WebDAV** (`bytemark/webdav`) — file server that hosts your `.kdbx` and
  speaks the WebDAV protocol that every native KeePass client supports
  (KeePassXC, KeePass2Android, Strongbox iOS). At
  `webdav.zachd.duckdns.org` over HTTPS, gated by **static HTTP Basic auth**
  with a credential in 1Password (`op://homelab/auth-keepass`).
- **KeeWeb** (`antelle/keeweb`) — browser-based KeePass UI for the "I'm on
  a random computer on the tailnet and need a password" case. At
  `keepass.zachd.duckdns.org`, gated by **Authelia forward-auth** (full
  OIDC + TOTP).

**Neither host is on the public internet.** Both `*.zachd.duckdns.org` names
resolve to a tailnet address, so reaching either one means being on the tailnet
first. `keepass.diemer.codes` used to publish KeeWeb through the Cloudflare
tunnel and was removed: Authelia in front of it was a real gate, but it made a
browser anywhere in the world one password-plus-TOTP away from the password
database. The vault is now behind a single boundary rather than two of
differing strength. Anything below that reads as "public" means "reachable on
the tailnet".

The `.kdbx` file is encrypted by your KeePass master password regardless of
which client opens it. The network-level auth in front of WebDAV is
gatekeeping access to an already-encrypted blob — so even if someone
guessed the WebDAV password they'd still hit the master-password wall.

## Why two different auth models?

Native KeePass apps can't follow an OIDC redirect — they speak HTTP Basic
on every WebDAV request and that's it. So WebDAV uses static Basic auth.
KeeWeb is a browser app, so it can do the full OIDC dance and gets gated
by Authelia just like RomM or any other browser-facing service.

If you ever want to unify the WebDAV password into Authelia's user DB,
swap the static Basic for an Authelia forward-auth Middleware with
`policy: one_factor` — the wire protocol is the same, only the validator
changes. Not done by default since it adds coupling for little real
benefit (one user, one password either way).

## Key file (optional second factor)

If you unlock your `.kdbx` with both a master password *and* a KeePass
key file, the chart can mount the key file into the **KeeWeb pod only**
as a static asset — never into the WebDAV pod. That's load-bearing: a
key file co-located with the `.kdbx` defeats its purpose, since an
attacker who gets at WebDAV would get both. The KeeWeb-side mount is
gated by Authelia forward-auth, so the key file is only fetchable after
a full OIDC + TOTP sign-in.

For native clients (KeePassXC, KeePass2Android, Strongbox), keep the
key file on-device — do not pull it from this URL on a phone or
desktop. The whole point of a key file is "lives somewhere different
from the .kdbx," and an Authelia password is not that separation.

To enable, base64-encode the key file and put it in the 1Password item
(`op://homelab/auth-keepass/values.local.yaml`, see [§3](#3-store-the-credential-in-1password)):

```yaml
keeweb:
  keyfile:
    enabled: true
    filename: "keyfile.bin"
    contentBase64: "<base64 -w0 < /path/to/your.keyx>"
```

After `helm install` (or `upgrade.sh`), the file is reachable at
`https://keepass.zachd.duckdns.org/keyfile.bin` once Authelia waves you
through. KeeWeb's flow: download it once per browser, then in KeeWeb's
"Open" dialog choose it as the key file alongside your `.kdbx`. KeeWeb
caches it in localStorage; subsequent vault opens just need the master
password.

---

## Prerequisites

### 1. Authelia must be installed first

The KeeWeb Ingress references the Traefik Middleware
`auth-authelia-forwardauth@kubernetescrd` deployed by `../authelia/`.
Helm will install fine without it, but Traefik will refuse the route
until the Middleware exists. Install Authelia first (see
`../authelia/README.md`).

### 2. Generate a WebDAV password

```bash
openssl rand -hex 24
```

This is what your KeePassXC / KeePass2Android / Strongbox installs will
use to authenticate to the WebDAV server. It lives in 1Password, **not** in
the `.kdbx` — storing it in the vault it unlocks means needing the vault to
fetch the vault, and a laptop that has lost its local copy could not get one.

### 3. Store the credential in 1Password

`values.local.yaml` is **not a file on disk** — `upgrade.sh` resolves
`values.local.tpl.yaml` (`op://homelab/auth-keepass/values.local.yaml`) into
memory for the life of the run via `scripts/lib/secret-values.sh`. Edit the
item in the vault; `values.local.yaml.example` shows the shape.

---

## First install

```bash
# Resolves the 1Password values into memory, then installs.
RELEASE=keepass ./auth/keepass/upgrade.sh

kubectl -n auth get pods -w
```

Both pods should reach `Running` in <30s. The WebDAV PVC starts empty;
you upload your `.kdbx` next.

### Upload your existing `.kdbx`

Pick one:

**A. curl PUT** (simplest):

```bash
curl --user 'zachd:<webdav-password>' \
  --upload-file /path/to/your-existing.kdbx \
  https://webdav.zachd.duckdns.org/passwords.kdbx
```

**B. KeePassXC "Save As":** Database → Save Database As → enter the URL
`https://webdav.zachd.duckdns.org/passwords.kdbx`. KeePassXC will prompt
for the WebDAV credentials.

Both leave the file at `/data/passwords.kdbx` inside the pod (persisted on
the PVC). The path was `/var/lib/dav/data/` under the old bytemark image;
hacdias serves the PVC root directly.

---

## Wiring native clients

### KeePassXC (desktop)

Database → Open Database → enter URL `https://webdav.zachd.duckdns.org/passwords.kdbx`,
provide the WebDAV credentials, then your KeePass master password.
KeePassXC remembers the URL.

### KeePass2Android (Android)

Open from URL → WebDAV → URL `https://webdav.zachd.duckdns.org/passwords.kdbx`,
WebDAV credentials, then master password.

### Strongbox (iOS)

Add Database → WebDAV → fill in the host/path/credentials. Strongbox
syncs in background.

---

## Wiring KeeWeb (browser)

1. Open <https://keepass.zachd.duckdns.org>. Authelia intercepts: sign
   in with your portal credentials + TOTP.
2. After Authelia waves you through, KeeWeb's UI loads.
3. (If using a key file) Download the key file once:
   `https://keepass.zachd.duckdns.org/keyfile.bin` → save to disk.
4. KeeWeb → "More" → "Open from URL" → paste
   `https://webdav.zachd.duckdns.org/passwords.kdbx`. Choose **WebDAV**
   as the storage type, provide the WebDAV credentials. In the unlock
   dialog, select your downloaded key file (if any) and enter the
   master password.
5. KeeWeb caches both URL and key file in browser localStorage;
   subsequent visits just prompt for the master password.

---

## Verification

```bash
# 1. Both pods Running
kubectl -n auth get pods -l app.kubernetes.io/instance=keepass

# 2. WebDAV reachable + auth works (expect 401 with no creds, 200 with)
curl -I https://webdav.zachd.duckdns.org/
curl -I --user 'zachd:<password>' https://webdav.zachd.duckdns.org/

# 3. KeeWeb hits the Authelia portal (expect 302 to auth.zachd)
curl -I https://keepass.zachd.duckdns.org/
```

---

## Backup

The whole vault is one file: `passwords.kdbx` on the WebDAV PVC. Periodic
copy is sufficient — the file is already master-password-encrypted. If
you use a key file, back that up separately (it's base64 in the 1Password
item and, once enabled, a Kubernetes Secret named `keepass-keyfile`).

```bash
# Copy the .kdbx out via curl (uses the same WebDAV credentials)
curl --user 'zachd:<password>' \
  -o "passwords-$(date -u +%Y%m%dT%H%M%SZ).kdbx" \
  https://webdav.zachd.duckdns.org/passwords.kdbx
```

`kubectl cp` and `kubectl exec` are **not** an option here, and no amount of
flag-fiddling will make them one: `ghcr.io/hacdias/webdav` is built `FROM
scratch`, so the container has no `ls`, no shell, and no `tar` for `cp` to
pipe through. Every exec attempt fails with `executable file not found in
$PATH`. curl over the WebDAV endpoint above is the way in; k8up (see below)
is the way that runs without you.

Automatic backups already cover this: the PVC is picked up by the weekly
`backup-auth` k8up schedule (Fridays 02:00, `infra/k8up`), which snapshots
`/data/keepass-webdav` into the restic repo. Verify a snapshot exists with:

```bash
kubectl -n auth get snapshots | grep keepass-webdav
```

---

## Upgrade

```bash
./auth/keepass/upgrade.sh
```

Same flow as every other chart in this repo. Safe for image bumps and
credential rotation. The `checksum/secret` annotation on the WebDAV
Deployment cycles the pod when the password changes.

---

## Uninstall

```bash
helm uninstall keepass -n auth
# PVC stays — your .kdbx and any client cache live there. To nuke:
kubectl -n auth delete pvc keepass-webdav
```

---

## Upstream

- WebDAV image: <https://github.com/BytemarkHosting/docker-webdav>
- KeeWeb: <https://keeweb.info> · <https://github.com/keeweb/keeweb>
- Authelia forward-auth: <https://www.authelia.com/integration/proxies/traefik/>
