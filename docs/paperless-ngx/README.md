# Paperless-ngx — self-hosted document management

Helm chart that runs [Paperless-ngx](https://docs.paperless-ngx.com) on the
home k3s cluster. Drop PDFs, images, and Office docs into the consume
folder; OCR runs, full-text search indexes them, and you get a tidy
web archive of every receipt, manual, and tax form.

This chart bundles everything paperless needs as inline Deployments — no
subchart dependencies:

- **paperless-ngx** — web + worker (single pod, `Recreate` strategy)
- **postgres** — durable backing store
- **redis** — Celery broker
- **gotenberg** — Office → PDF conversion (toggleable)
- **tika** — Office text/metadata extraction (toggleable)

v1 ships LAN-only. DuckDNS exposure and Authelia OIDC are tracked as
separate follow-ups below.

## Architecture

```
                    ┌────────────────────────────────────┐
                    │          paperless pod             │
 consume/  ─────►   │  web (port 8000) + Celery worker   │
 (PVC subPath)      │                                    │
                    └────┬──────────┬──────────┬─────────┘
                         │          │          │
                    ┌────▼────┐ ┌───▼──┐ ┌─────▼─────┐
                    │postgres │ │redis │ │ tika +    │
                    │  (PVC)  │ │      │ │ gotenberg │
                    └─────────┘ └──────┘ └───────────┘
```

The paperless pod uses a single RWO PVC, subPathed into `data/`, `media/`,
`consume/`, and `export/`. Postgres has its own PVC. Redis is ephemeral.

---

## Prerequisites (one-time, manual)

### 1. Create the namespace

```bash
kubectl create namespace docs
```

### 2. Pick a timezone + OCR language

Default is `America/Los_Angeles` and English-only OCR. If you need
multi-language docs, set `paperless.ocrLanguage: "eng+spa"` (or whatever
combo) in `values.local.yaml`. The image bundles ~100 Tesseract languages —
no extra install step needed.

---

## First install

```bash
# 1. Populate values.local.yaml
cp docs/paperless-ngx/values.local.yaml.example docs/paperless-ngx/values.local.yaml
# Edit docs/paperless-ngx/values.local.yaml:
#   paperless.secretKey       = $(openssl rand -hex 32)
#   paperless.admin.user      = your username
#   paperless.admin.email     = your email
#   paperless.admin.password  = strong, change after first login
#   postgres.password         = $(openssl rand -hex 24)

# 2. Install
helm install paperless ./docs/paperless-ngx -n docs \
  -f docs/paperless-ngx/values.yaml \
  -f docs/paperless-ngx/values.local.yaml

# 3. Watch pods
kubectl -n docs get pods -w
```

First boot is the slowest one. Postgres initdb runs, paperless applies
~50 Django migrations, then the bootstrap superuser is created. Plan on
~2-3 minutes before the web UI answers.

### Accessing it (LAN-only, v1)

No Ingress until the DuckDNS follow-up. Use port-forward:

```bash
kubectl -n docs port-forward svc/paperless 8000:8000
# Open http://localhost:8000
```

For LAN-wide access from another box, either port-forward with
`--address 0.0.0.0` from a gateway machine, or temporarily flip the
Service to `NodePort` (`service.type: NodePort` in `values.local.yaml`)
and re-run `upgrade.sh`.

### After first login

1. Sign in with the bootstrap admin from `values.local.yaml`.
2. Change the password in **Settings → Users**.
3. **Remove** `paperless.admin.password` from `values.local.yaml` — the
   env var only seeds the user when the table is empty, but leaving the
   plaintext sitting in your local file is unnecessary risk.
4. Re-run `./docs/paperless-ngx/upgrade.sh` so the rendered Secret no
   longer carries the password.

---

## Verification

```bash
# 1. All pods Running
kubectl -n docs get pods
#   NAME                                READY   STATUS
#   paperless-<hash>                    1/1     Running
#   paperless-postgres-<hash>           1/1     Running
#   paperless-redis-<hash>              1/1     Running
#   paperless-gotenberg-<hash>          1/1     Running    # if tikaEnabled
#   paperless-tika-<hash>               1/1     Running    # if tikaEnabled

# 2. DB connection from paperless pod
kubectl -n docs exec deploy/paperless -- \
  python manage.py check --database default

# 3. Drop a test PDF in the consume folder via kubectl cp
kubectl -n docs cp ./test.pdf deploy/paperless:/usr/src/paperless/consume/

# 4. Watch the worker pick it up
kubectl -n docs logs deploy/paperless | grep -i consume
```

Once it's processed, the file disappears from `consume/` and shows up in
the web UI's Documents view.

---

## Upgrade

```bash
./docs/paperless-ngx/upgrade.sh
```

Wraps `helm upgrade` + rollout waits for paperless, postgres, and redis.
Safe to run after bumping any image tag or editing `values.local.yaml`.
The `checksum/env` and `checksum/postgres` annotations on the paperless
Deployment pick up Secret changes and force a pod restart.

### Bumping the paperless-ngx image

Paperless-ngx applies DB migrations on every boot. The new image needs to
be migration-compatible with the on-disk Postgres data. For minor releases
(2.14 → 2.15) this is a non-event; for major (2.x → 3.x) check the
upstream release notes for breaking changes before rolling.

---

## Uninstall

```bash
helm uninstall paperless -n docs
# PVCs stay so reinstall keeps the document archive + DB. To nuke everything:
kubectl -n docs delete pvc paperless-data paperless-postgres
```

---

## Trimming compute (optional)

Tika + Gotenberg together account for ~600 MiB of resident memory and one
extra CPU core under load. If you don't need to OCR Office docs, disable
both in `values.local.yaml`:

```yaml
paperless:
  tikaEnabled: false
```

Re-run `upgrade.sh`. PDFs and images still OCR fine; `.docx`/`.odt`/etc.
become unsupported file types and won't be ingested.

---

## Follow-up A: Ingress at docs.zachd.duckdns.org

Until this is done, paperless is LAN-only via port-forward.

### How DNS + TLS already work in this cluster

DuckDNS resolves any `*.zachd.duckdns.org` query to the same A record as
the parent domain, so `docs.zachd.duckdns.org` already resolves today —
no DuckDNS account changes and no edits to talaria's `duckdns-updater`
CronJob (which only refreshes the `zachd` record). See
`minecraft/bluemap-ingress.yaml` for the same pattern in action at
`map.zachd.duckdns.org`.

For TLS, talaria's Traefik holds a wildcard `*.zachd.duckdns.org` cert
issued via DNS-01 (the only ACME challenge DuckDNS supports — it can
only write TXT at the account's top-level subdomain). Every Ingress in
this cluster requests that same wildcard SAN via the
`router.tls.domains.0.main` + `sans` annotations, so new hosts under
`*.zachd.duckdns.org` serve from the cached cert without a second ACME
round-trip. The chart's `templates/ingress.yaml` already does this.

### Steps

1. **Flip paperless's Ingress on.** In `docs/paperless-ngx/values.local.yaml`:

   ```yaml
   paperless:
     url: "https://docs.zachd.duckdns.org"   # paperless uses this for absolute links

   ingress:
     enabled: true
     host: docs.zachd.duckdns.org
   ```

   Re-run `./docs/paperless-ngx/upgrade.sh`. Traefik picks up the new
   Ingress, matches it against the cached wildcard, and starts serving.

2. **Keep it LAN-only with a real cert.** If you don't want to expose
   paperless to the public internet, either add a Traefik IP-allowlist
   middleware referenced from the Ingress annotations, or skip the
   Ingress entirely and use split-horizon DNS (AdGuard rewrite) pointing
   `docs.zachd.duckdns.org` at the cluster's LAN IP — no cert required
   on plain HTTP for LAN-only use.

---

## Follow-up B: Authelia OIDC (shared sign-in)

See `../../auth/authelia/README.md` for standing up Authelia. Once it's
reachable at `https://auth.zachd.duckdns.org` with a real cert, wire
paperless:

1. **Register paperless as an OIDC client in Authelia.** In Authelia's
   `values.local.yaml`, under `oidc.clients`:

   ```yaml
   - id: paperless
     description: Paperless-ngx
     secret: "$pbkdf2-sha512$..."   # hash with: authelia crypto hash generate pbkdf2 --variant sha512
     public: false
     authorization_policy: two_factor
     redirect_uris:
       - https://docs.zachd.duckdns.org/accounts/oidc/authelia/login/callback/
     scopes: [openid, profile, email]
     userinfo_signing_algorithm: none
   ```

   Keep the plaintext client secret around — paperless needs it.

2. **Enable OIDC in paperless.** In `docs/paperless-ngx/values.local.yaml`:

   ```yaml
   paperless:
     oidc:
       enabled: true
       name: "Authelia"
       issuer: "https://auth.zachd.duckdns.org"
       clientId: "paperless"
       clientSecret: "<plaintext, matching Authelia's hash>"
   ```

   `./docs/paperless-ngx/upgrade.sh`. The login page now shows an
   "Authelia" button. First sign-in for each user creates a local
   paperless user record with the email from the IDP.

3. **Make the local admin a superuser binding.** Paperless's first-OIDC-
   login user lands as a regular user. Either:
   - Sign in via OIDC first, then have the bootstrap admin promote that
     account to staff/superuser in **Settings → Users**, or
   - Match the OIDC username to the bootstrap admin so the existing
     superuser row is reused.

---

## Directory layout inside the container

```
/usr/src/paperless/
  consume/         ← drop files here for ingestion       (data PVC subPath)
  data/            ← search index, app state             (data PVC subPath)
  media/           ← original + archive copies of docs   (data PVC subPath)
  export/          ← `document_exporter` output target   (data PVC subPath)
```

Postgres data lives on its own PVC at `/var/lib/postgresql/data/pgdata`.
Redis is ephemeral (no AOF, no RDB) — the broker is for in-flight tasks
only and paperless retries cleanly across restarts.

---

## Upstream

- Paperless-ngx: <https://github.com/paperless-ngx/paperless-ngx>
- Docs: <https://docs.paperless-ngx.com>
- Tika: <https://tika.apache.org>
- Gotenberg: <https://gotenberg.dev>
