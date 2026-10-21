# Stirling PDF

PDF editing, conversion, and OCR. The image's `-fat` variant includes the external
conversion tools. `/configs` persists settings, pipelines, signing material, and
native user accounts; processed documents are temporary, not a document archive.

## Configuration and deployment

Use [values.yaml](values.yaml) for supported settings and
[values.local.yaml.example](values.local.yaml.example) for the secret document's
shape. Use [the shared secrets workflow](../code-tree-CLAUDE.md) instead of writing
a plaintext `values.local.yaml`.

For an existing release, from the repository root:

```sh
eval "$(scripts/op-session.sh ensure)"
docs/stirling-pdf/upgrade.sh
```

For first installation, create namespace `docs`, load this chart's secret values
with `scripts/lib/secret-values.sh` (`sv_load docs/stirling-pdf`), and use
`helm install stirling docs/stirling-pdf -n docs -f docs/stirling-pdf/values.yaml -f <(sv_fd)`.
The upgrade script expects the release to exist.

## Access and authentication

With ingress disabled, use `kubectl -n docs port-forward deploy/stirling 8080:8080`.
Enable ingress only with an authentication method configured.

| Method | Configuration |
|---|---|
| Authelia forward-auth | Default: `auth.forwardAuth.enabled=true`; native login off |
| Native accounts | Disable forward-auth, enable `stirling.login`, configure bootstrap credentials in the vault |
| Native OIDC | Disable forward-auth, enable `stirling.oauth2`, register the matching client in Authelia |

The forward-auth middleware lives in namespace `docs` and points to Authelia's
service; cross-namespace middleware access is not required. The ingress host is
`pdf.zachd.duckdns.org`, with public tunnel aliases configured through
`ingress.cloudflareHosts`.

Native login requires the configs PVC. Rotate bootstrap credentials in the UI
and remove them from the vault afterward. For OIDC, the callback is
`https://pdf.zachd.duckdns.org/login/oauth2/code/oidc`; issuer, client ID, secret,
and registered redirect URI must agree. The default username claim is
`preferred_username`. See [Authelia](../../auth/authelia/README.md) and its
[Stirling integration guide](https://www.authelia.com/integration/openid-connect/clients/stirling-pdf/).

## Verification and recovery

Check pod readiness and logs, verify the ingress requires authentication, and
exercise a PDF operation plus Office conversion if used. First image pulls and
JVM startup can take longer than ordinary restarts.

Back up `/configs` before upgrades. Image pins belong in `values.yaml` and chart
metadata. Uninstalling the release is separate from deleting its persistent
settings and signing keys; retain the PVC for recovery.

See [upstream configuration](https://docs.stirlingpdf.com/Configuration/) for
settings beyond the chart's options.
