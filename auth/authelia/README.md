# Authelia — self-hosted auth server

Helm chart that runs [Authelia](https://www.authelia.com) as the shared
OIDC provider for every other service in the cluster (RomM first, others
later). File-based user DB + SQLite storage — no LDAP, no Postgres, no
Redis. Fits a small friend group; can be swapped out later.

Authelia also supports Traefik **forward-auth** (drop a middleware in
front of any service, even ones that don't speak OIDC), but this chart
wires OIDC only to start. Add the middleware in a follow-up when you want
to protect, e.g., Beszel's dashboard.

---

## Prerequisites (must be done before `helm install`)

### 1. DuckDNS follow-up from the RomM README

Authelia's OIDC issuer URL **must be HTTPS** — RomM's (and every other)
OIDC client refuses HTTP issuers. That means the DuckDNS + wildcard-cert
work described in `../../games/romm/README.md` §"Follow-up A" needs to
land before Authelia is useful. Specifically:

- `auth.zachd` added as a sub-subdomain in DuckDNS.
- `talaria`'s DuckDNS updater CronJob extended to include
  `auth.zachd` in its `DUCKDNS_DOMAIN` list.
- Traefik already holds a wildcard `*.zachd.duckdns.org` cert (which it
  will once any service Ingress asks for it).

You *can* `helm install` Authelia with `ingress.enabled: false` first to
confirm the pod boots, but you can't actually sign in until the Ingress
is on and the hostname resolves with a valid cert.

### 2. Generate one-time secrets

All of these go into `values.local.yaml` (gitignored). **Do this once and
never rotate casually** — rotating the session secret signs everyone out,
rotating the OIDC HMAC or issuer key invalidates every OIDC token and
every consent record.

```bash
# High-entropy symmetric secrets — one each:
openssl rand -hex 64     # secrets.jwtSecret
openssl rand -hex 64     # secrets.sessionSecret
openssl rand -hex 64     # secrets.storageEncryptionKey
openssl rand -hex 64     # oidc.hmacSecret

# OIDC id_token signing key (paste the full PEM into oidc.issuerPrivateKey):
openssl genrsa 4096
```

### 3. SMTP credentials

Authelia emails users at 2FA enrollment and for password resets. Any
relay works. For Gmail specifically you want a **Google app password**,
not your account password — generate one at
<https://myaccount.google.com/apppasswords> after enabling 2FA.

Non-Gmail options that work painlessly: Fastmail, SendGrid (free tier),
Mailgun, Postmark.

### 4. Hash your first user's password

```bash
docker run --rm authelia/authelia:4.38.17 \
  authelia crypto hash generate argon2 \
  --password 'the-password-you-want-to-use'
```

Paste the `$argon2id$...` output into the `users.zachd.password` field.

### 5. Create the namespace

```bash
kubectl create namespace auth
```

---

## First install

```bash
# 1. Populate values.local.yaml
cp auth/authelia/values.local.yaml.example auth/authelia/values.local.yaml
# Edit auth/authelia/values.local.yaml per §Prerequisites above.

# 2. Install
helm install authelia ./auth/authelia -n auth \
  -f auth/authelia/values.yaml \
  -f auth/authelia/values.local.yaml

# 3. Watch the pod come up
kubectl -n auth get pods -w
```

### Flip the Ingress on (requires DuckDNS follow-up done)

Once `auth.zachd.duckdns.org` exists in DuckDNS and resolves to your
public IP, set `ingress.enabled: true` in `values.local.yaml` and
`./auth/authelia/upgrade.sh`. Traefik will mint/reuse the wildcard cert
and start routing. Open <https://auth.zachd.duckdns.org> — you should
see Authelia's login screen.

First sign-in will walk you through TOTP enrollment (Google
Authenticator, Aegis, 1Password, etc. all work). The enrollment link
arrives by email — that's why SMTP has to work before first login.

---

## Verification

```bash
# 1. Pod Running + healthy
kubectl -n auth get pods
kubectl -n auth logs deploy/authelia | tail

# 2. Config parsed without errors (Authelia is strict — typos fail hard)
kubectl -n auth logs deploy/authelia | grep -iE "error|fatal" || echo "clean"

# 3. Portal loads and SMTP works — trigger a password reset and confirm
#    the mail lands.
```

---

## Wiring RomM (or any OIDC client)

1. **Pick a plaintext client secret** for RomM. Generate with
   `openssl rand -hex 32`. You'll paste the plaintext into RomM's
   `values.local.yaml`; Authelia only gets the hash.

2. **Hash it for Authelia:**

   ```bash
   docker run --rm authelia/authelia:4.38.17 \
     authelia crypto hash generate pbkdf2 --variant sha512 \
     --password 'the-plaintext-secret-you-just-generated'
   ```

3. **Register the client** in `auth/authelia/values.local.yaml` under
   `oidc.clients`:

   ```yaml
   oidc:
     clients:
       - id: romm
         description: RomM
         secret: "$pbkdf2-sha512$310000$..."   # hash from step 2
         public: false
         authorization_policy: two_factor
         redirect_uris:
           - https://romm.zachd.duckdns.org/api/oauth/openid
         scopes: [openid, profile, email, groups]
         userinfo_signing_algorithm: none
   ```

   `./auth/authelia/upgrade.sh` to pick it up.

4. **Enable OIDC on RomM's side** — see
   `../../games/romm/README.md` §"Follow-up B" for the RomM-side values.

5. **Test the flow:** sign out of RomM, click the "Authelia" button on
   the login screen. You should redirect to Authelia, authenticate,
   consent, and land back in RomM signed in. RomM will lazily create a
   local user row the first time each person signs in via OIDC.

---

## Adding friends

1. Pick a username.
2. Have them pick a password. Hash it with the Docker one-liner in
   §Prerequisites step 4.
3. Add a block to `users.<username>` in `values.local.yaml`:

   ```yaml
   users:
     friendname:
       displayname: "Friend Name"
       email: "friend@example.com"
       groups: [users]
       password: "$argon2id$v=19$m=65536,t=3,p=4$..."
   ```

4. `./auth/authelia/upgrade.sh`. The `checksum/users` annotation picks
   up the Secret change and cycles the pod (~10s).

5. Friend signs in at <https://auth.zachd.duckdns.org>, enrolls TOTP
   (prompted on first login via email link), then uses any protected
   service.

---

## Upgrade

```bash
./auth/authelia/upgrade.sh
```

Same flow as every other chart in this repo. Safe for image bumps,
user additions, client additions, rule changes.

---

## Uninstall

```bash
helm uninstall authelia -n auth
# Data PVC (2FA registrations, OIDC consent history) stays. To nuke:
kubectl -n auth delete pvc authelia-data
```

Users get signed out of every service that trusts Authelia, and will
need to re-enroll TOTP on next sign-in since the storage DB is gone.

---

## Follow-up: Traefik forward-auth for non-OIDC services

Authelia can also protect any HTTP service via a Traefik middleware —
useful for admin UIs that don't speak OIDC (AdGuard Home, Beszel,
kubectl dashboards, etc.). Sketch: add a
`Middleware` CRD pointing at Authelia's `/api/verify?rd=...` endpoint,
then reference it from the target service's Ingress annotations. Not
wired in this chart yet; raise as a task when you need it.

---

## Upstream

- Authelia: <https://github.com/authelia/authelia>
- Docs: <https://www.authelia.com/docs/>
- OIDC provider docs: <https://www.authelia.com/configuration/identity-providers/open-id-connect/>
