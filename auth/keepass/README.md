# KeePass — self-hosted vault, accessible across devices

Helm chart that runs two complementary pods in the `auth` namespace:

- **WebDAV** (`bytemark/webdav`) — file server that hosts your `.kdbx` and
  speaks the WebDAV protocol that every native KeePass client supports
  (KeePassXC, KeePass2Android, Strongbox iOS). Public at
  `webdav.zachd.duckdns.org` over HTTPS, gated by **static HTTP Basic auth**
  with a credential set in `values.local.yaml`.
- **KeeWeb** (`antelle/keeweb`) — browser-based KeePass UI for the "I'm on
  a random computer and need a password" case. Public at
  `keepass.zachd.duckdns.org`, gated by **Authelia forward-auth** (full
  OIDC + TOTP).

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

To enable, base64-encode the key file and paste it into
`values.local.yaml`:

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
use to authenticate to the WebDAV server. Save it somewhere safe —
ideally in the KeePass database itself once it's set up.

### 3. Populate `values.local.yaml`

```bash
cp auth/keepass/values.local.yaml.example auth/keepass/values.local.yaml
# Edit auth/keepass/values.local.yaml — fill in webdav.auth.username
# and webdav.auth.password.
```

The file is gitignored (repo-root `**/values.local.yaml`).

---

## First install

```bash
helm install keepass ./auth/keepass -n auth \
  -f auth/keepass/values.yaml \
  -f auth/keepass/values.local.yaml

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

Both leave the file at `/var/lib/dav/data/passwords.kdbx` inside the pod
(persisted on the PVC).

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
you use a key file, back that up separately (it's in `values.local.yaml`
as base64 and as a Kubernetes Secret named `keepass-keyfile`).

```bash
# Copy the .kdbx out via curl (uses the same WebDAV credentials)
curl --user 'zachd:<password>' \
  -o "passwords-$(date -u +%Y%m%dT%H%M%SZ).kdbx" \
  https://webdav.zachd.duckdns.org/passwords.kdbx
```

Or pull it directly from the pod:

```bash
POD=$(kubectl -n auth get pod -l app.kubernetes.io/component=webdav -o name | head -1)
kubectl -n auth cp "${POD#pod/}:/var/lib/dav/data/passwords.kdbx" \
  "passwords-$(date -u +%Y%m%dT%H%M%SZ).kdbx"
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
