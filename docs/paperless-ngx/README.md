# Paperless-ngx

Document ingestion, OCR, and search. The chart runs a single Paperless web/worker
pod with PostgreSQL, ephemeral Redis, and optional Tika/Gotenberg converters.
Paperless uses a data PVC with `data`, `media`, `consume`, and `export` subpaths;
PostgreSQL has a separate PVC. Deployment uses `Recreate` for the single data volume.

## Configuration and deployment

Use [values.yaml](values.yaml) for supported settings and
[values.local.yaml.example](values.local.yaml.example) for the secret document's
shape. Resolve secrets through [the shared workflow](../code-tree-CLAUDE.md);
do not create a plaintext `values.local.yaml`.

Configure the Paperless secret key, database password, bootstrap admin, public
URL, timezone, and OCR language before installation. Keep the secret key stable
across upgrades. The bootstrap account is for first installation; after changing
its password in the UI, remove the bootstrap password from the vault configuration.

For an existing release, from the repository root:

```sh
eval "$(scripts/op-session.sh ensure)"
docs/paperless-ngx/upgrade.sh
```

For first installation, create namespace `docs`, load this chart's secret values
with `scripts/lib/secret-values.sh` (`sv_load docs/paperless-ngx`), and use
`helm install paperless docs/paperless-ngx -n docs -f docs/paperless-ngx/values.yaml -f <(sv_fd)`.
The upgrade script expects the release to exist.

## Access and authentication

With ingress disabled, use:

```sh
kubectl -n docs port-forward svc/paperless 8000:8000
```

For ingress, set `ingress.enabled`, `paperless.url`, and any trusted-origin
configuration consistently. Personal documents use the private
`docs.zachd.duckdns.org` route; `ingress.cloudflareHosts` is empty. Adding
forward-auth can break mobile token authentication.

Native OIDC is available through `paperless.oidc`. Register the client with
Authelia and match its issuer, client ID, secret, and Paperless callback URL.
See [Authelia](../../auth/authelia/README.md) and
[Paperless configuration](https://docs.paperless-ngx.com/configuration/).

## Verification and recovery

Check pod readiness and application logs, then import a disposable document and
verify OCR, search, and download. Test Office conversion if Tika/Gotenberg is enabled.

Back up both the document data and PostgreSQL before upgrades. Paperless runs
schema migrations at startup; allow migrations to finish before restarting it.
An image rollback may require restoring the matching database. Use
[K8up](../../infra/k8up/README.md) for backup/restore procedures, and retain PVCs
when uninstalling unless their data is intentionally being discarded.

## Internal service isolation

NetworkPolicies allow only this release's Paperless application pods to reach
Redis, PostgreSQL, Tika, and Gotenberg. In particular, Redis is a trusted Celery
broker accepting pickle tasks; it must never be reachable by unrelated workloads.
These policies preserve Paperless's existing serializer and scheduled tasks.
