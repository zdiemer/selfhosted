# Stirling PDF — self-hosted PDF toolkit

Helm chart that runs [Stirling PDF](https://github.com/Stirling-Tools/Stirling-PDF):
a web UI for ~50 PDF operations (merge/split, convert to/from Office &
images, OCR, sign, redact, stamp, compress, repair, …). Everything is
processed **locally on the node** — nothing is sent to a third party.

Single deployment, one small `/configs` PVC, no database. Lands in the
existing **`docs`** namespace alongside [`paperless-ngx`](../paperless-ngx/).

---

## Does it need auth gating?

**Yes — if you expose it publicly, it must be gated.** Stirling PDF ships
**no authentication by default**: the bare app is wide open. Behind your LAN
that's fine, but on a public `*.zachd.duckdns.org` host it would be a PDF
processor (and arbitrary-file-upload endpoint) that *anyone on the internet*
can drive — resource abuse at best, a free anonymous file-handling service at
worst. So the chart does not expose it unguarded.

Three ways to gate it, in order of how this chart prefers them:

| Approach | How | When |
|---|---|---|
| **Authelia forward-auth** *(default)* | Traefik Middleware bounces every request through Authelia before it hits Stirling. Stirling stays auth-less *inside* the cluster; the gate is at the edge. | You just want "only my Authelia users can reach it." No Stirling-side config, no second account system. **This is what's wired by default.** |
| **Stirling native OIDC** | Stirling renders its own login page with an "Authelia" button and does the OAuth2 dance. | You want Stirling to know *who* each user is (per-user settings/quotas). See [§Follow-up B](#follow-up-b--native-oidc-instead-of-forward-auth). |
| **Stirling native login** | Stirling's built-in username/password DB in `/configs`. | You don't run Authelia, or want a standalone shared password. See [§Follow-up A](#follow-up-a--stirling-native-login). |

The default (forward-auth) reuses the [`auth/authelia`](../../auth/authelia/)
chart already in this cluster — Stirling is the first consumer of the
forward-auth pattern that the Authelia README flagged for "admin UIs that don't
speak OIDC."

> If you run with `ingress.enabled: true` **and**
> `auth.forwardAuth.enabled: false` **and** no native login/OIDC, you've
> published an open PDF processor. Don't.

---

## Prerequisites

- The `docs` namespace already exists (created for paperless-ngx). If not:
  `kubectl create namespace docs`.
- For the default forward-auth gate: the `authelia` release must be running
  in the `auth` namespace (it is — see [`auth/authelia`](../../auth/authelia/)).
- DNS/TLS: nothing to do. `pdf.zachd.duckdns.org` already resolves via the
  DuckDNS wildcard, and Traefik already holds the `*.zachd.duckdns.org` cert.
  See [`auth/authelia/README.md`](../../auth/authelia/README.md) §1 for the
  full explanation of the wildcard-cert pattern.

---

## First install (LAN-only, no ingress)

Bring it up internal-only first to confirm the pod is healthy:

```bash
# 1. (optional) seed local values
cp docs/stirling-pdf/values.local.yaml.example docs/stirling-pdf/values.local.yaml
# Leave ingress.enabled commented out / false for this step.

# 2. Install
helm install stirling ./docs/stirling-pdf -n docs \
  -f docs/stirling-pdf/values.yaml \
  -f docs/stirling-pdf/values.local.yaml   # omit -f if you skipped step 1

# 3. Watch it come up (JVM boot + LibreOffice warm-up = ~30–60s)
kubectl -n docs get pods -l app.kubernetes.io/instance=stirling -w

# 4. Smoke-test the UI without exposing it
kubectl -n docs port-forward deploy/stirling 8080:8080
# open http://localhost:8080
```

The `:2.11.0-fat` image is large (~2–3 GB, it bundles LibreOffice + Calibre +
full Tesseract). First pull is slow; subsequent restarts are cached.

---

## Expose via DuckDNS

`pdf.zachd.duckdns.org` already resolves and the wildcard cert already exists,
so exposing is just flipping the ingress on — the Authelia gate comes along
automatically (`auth.forwardAuth.enabled` defaults to `true`):

```bash
# In values.local.yaml:
#   ingress:
#     enabled: true

./docs/stirling-pdf/upgrade.sh
```

This renders:
- an **Ingress** for `pdf.zachd.duckdns.org` (websecure + duckdns wildcard cert),
- a Traefik **Middleware** (`docs-stirling-forwardauth`) pointing at Authelia,
- the Ingress annotation wiring that middleware in front of the route.

Open <https://pdf.zachd.duckdns.org> → you should be redirected to the Authelia
portal, authenticate (TOTP), then land in Stirling.

> **Access policy.** Authelia's `default_policy` is `two_factor`, so
> `pdf.zachd.duckdns.org` is gated even without an explicit rule. To make it
> explicit (or to loosen/tighten), add a rule under `accessControl.rules` in
> [`auth/authelia/values.yaml`](../../auth/authelia/values.yaml):
>
> ```yaml
> - domain: "pdf.zachd.duckdns.org"
>   policy: "two_factor"
> ```
>
> then `./auth/authelia/upgrade.sh`.

> **Cross-namespace note.** The forward-auth Middleware is rendered into the
> `docs` namespace (it just points at the Authelia *Service* in `auth` by URL),
> so this works **without** enabling Traefik's `allowCrossNamespace`. If you'd
> rather reuse Authelia's own middleware in the `auth` namespace, you'd have to
> turn that flag on in the Traefik config instead — this chart avoids that.

---

## Verification

```bash
# Pod Running + healthy
kubectl -n docs get pods -l app.kubernetes.io/instance=stirling
kubectl -n docs logs deploy/stirling | tail

# Middleware + ingress exist once exposed
kubectl -n docs get middleware,ingress -l app.kubernetes.io/instance=stirling

# The gate works: this should 302 to auth.zachd.duckdns.org, NOT 200 from Stirling
curl -sI https://pdf.zachd.duckdns.org | grep -i location
```

---

## Follow-up A — Stirling native login

If you'd rather not gate at the edge (e.g. no Authelia), use Stirling's own
account DB. In `values.local.yaml`:

```yaml
auth:
  forwardAuth:
    enabled: false        # don't double-gate
stirling:
  login:
    enabled: true
    initialUsername: "admin"
    initialPassword: "set-a-strong-one-then-rotate-in-the-UI"
```

`./docs/stirling-pdf/upgrade.sh`. The user DB lives in the `/configs` PVC, so
keep `persistence.configs.enabled: true`. Change the password in the UI after
first login and clear it from `values.local.yaml`.

---

## Follow-up B — native OIDC instead of forward-auth

To have Stirling itself do the OAuth2 dance with Authelia (so it knows each
user's identity):

1. **Register the client in Authelia.** Pick a plaintext secret
   (`openssl rand -hex 32`), hash it, and add a client to
   `auth/authelia/values.local.yaml` — see
   [`auth/authelia/README.md`](../../auth/authelia/README.md) §"Wiring …".
   Stirling's redirect URI is:

   ```
   https://pdf.zachd.duckdns.org/login/oauth2/code/oidc
   ```

   ```yaml
   oidc:
     clients:
       - id: stirlingpdf
         description: Stirling PDF
         secret: "$pbkdf2-sha512$310000$..."   # the hash
         public: false
         authorization_policy: two_factor
         redirect_uris:
           - https://pdf.zachd.duckdns.org/login/oauth2/code/oidc
         scopes: [openid, profile, email, groups]
   ```

   `./auth/authelia/upgrade.sh`.

2. **Enable OIDC on Stirling's side**, and turn off forward-auth so the two
   don't double-prompt. In `values.local.yaml`:

   ```yaml
   auth:
     forwardAuth:
       enabled: false
   stirling:
     oauth2:
       enabled: true
       issuer: "https://auth.zachd.duckdns.org"
       clientId: "stirlingpdf"
       clientSecret: "the-plaintext-secret-you-registered"
   ```

   `./docs/stirling-pdf/upgrade.sh`.

> Stirling's OIDC support has historically been finicky about the username
> claim — `SECURITY_OAUTH2_USEASUSERNAME` defaults to `preferred_username`
> here, which Authelia emits. If logins land as blank/garbled usernames, see
> the Authelia ↔ Stirling integration guide:
> <https://www.authelia.com/integration/openid-connect/clients/stirling-pdf/>

---

## Upgrade

```bash
./docs/stirling-pdf/upgrade.sh
```

Bump `image.tag` (and `appVersion` in `Chart.yaml`) for a version upgrade. Per
the cluster convention, prefer the script over raw `helm upgrade`.

---

## Uninstall

```bash
helm uninstall stirling -n docs
# /configs PVC (settings, pipelines, signing keys, user DB) stays. To nuke:
kubectl -n docs delete pvc stirling-configs
```

---

## Upstream

- Stirling PDF: <https://github.com/Stirling-Tools/Stirling-PDF>
- Docs: <https://docs.stirlingpdf.com>
- Configuration reference: <https://docs.stirlingpdf.com/Configuration/>
