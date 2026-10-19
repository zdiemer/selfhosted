# CrowdSec log acquisition

The Helm release uses the upstream chart with `values.yaml` and vault overrides.
Run `./upgrade.sh` to apply it, then wait for the agents as well as LAPI:

```sh
kubectl -n crowdsec rollout status daemonset/crowdsec-agent --timeout=10m
```

## Partial-line backport (2026-10-19)

CrowdSec v1.7.8's file tailer can emit a half-written line at EOF, then skip its
remainder and subsequent lines. This caused truncated Traefik JSON errors.
Upstream fixed it in [PR #4678](https://github.com/crowdsecurity/crowdsec/pull/4678)
(commit `e63ec0f101786f7d804c404a2f2b8fc744889f33`), but stable v1.8.1 also lacks
that fix. `image/Dockerfile` backports `CompleteLines: true` onto v1.7.8; it keeps
the upstream runtime, configuration, cscli and plugins. Go builds are limited to
two concurrent compiler processes. Replace the custom image with an upstream
stable release once that release includes the fix.

```sh
docker buildx build --builder renovate-builder --platform linux/amd64 \
  --target test image
docker buildx build --builder renovate-builder --platform linux/amd64 \
  -t registry.zachd.duckdns.org/zdiemer/crowdsec:v1.7.8-complete-lines1 --push image
```

The optional test target runs upstream's partial-write and truncation regression
against the patched source. Only the `traefik` container is acquired; the former
pod wildcard also included certificate Jobs. `poll_without_inotify: true` follows
Kubernetes' rotating symlink targets, as recommended by the file datasource.
The existing parsers, scenarios, whitelists, decisions and notification settings
are preserved. Do not hide parse failures with a blanket filter.

## Notification routing

Non-routine remediation notifications still call the durable relay with the
existing `crowdsec` API key; its recipient-scoped policy now publishes to ntfy's
`security` topic first and selects SMS only after publish failure. The HTTP
plugin aggregates for five minutes or 20 alerts; the existing summary format
reports the first event and additional count. Routine scanner suppressions are
unchanged. Configuration remains in the `infra-crowdsec` vault item; no second
SMS notifier was added. See [routing](../../docs/notification-routing.md).

### ForwardAuth warning noise (2026-10-19)

A sustained parser failure ratio came from Traefik service warnings mixed into
its access-log stream: every config reload warned about ForwardAuth middleware
without `maxResponseBodySize`. Every owned middleware now caps authentication
response bodies at 1 MiB. All 17 live middleware resources were patched and
their source templates updated (including Money, SMS relay and diemer.codes).
The recent parser-failure rate returned to zero without weakening the parser,
whitelisting arbitrary malformed access logs, or raising the alert threshold.

The limit is on the authentication server's **response**, not uploaded files
or the upstream application's response. Authelia redirects still work.
See [Traefik's ForwardAuth options](https://doc.traefik.io/traefik/v3.7/middlewares/http/forwardauth/).
