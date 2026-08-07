# ingress-policy

Enforces this cluster's Ingress conventions at admission, in every namespace —
including the ones owned by other repos.

Two cluster-scoped objects, no pods: a `ValidatingAdmissionPolicy` (the rules)
and a `ValidatingAdmissionPolicyBinding` (where they apply, and how hard).

## Why

Most ingress here comes from charts in this repo, which all copy the same
annotation block. But not all of it:

- **whatnowgg** deploys its dev environment into this cluster from a separate
  private repo, and redeploys every few hours.
- **talaria**'s chart lives in another repo entirely.
- **money** is a submodule that isn't even checked out.

A convention those charts cannot see is a convention that drifts — and it had.
whatnowgg's Ingress carried no TLS annotations and no entrypoint pin at all, so
Traefik served that host with its own self-signed certificate and bound the
router to every entrypoint. Nothing alerted. It just quietly looked broken to
anyone who visited.

Fixing that Ingress was easy. Making it impossible to reintroduce — from a repo
that will never read this one — is what this chart is for.

## The rules

| # | Requirement | Why |
|---|---|---|
| 1 | `spec.ingressClassName: traefik` | Otherwise the Ingress silently depends on whichever IngressClass is currently the cluster default — invisible in the manifest, and able to change under you. |
| 2 | `router.entrypoints: websecure` | Otherwise Traefik binds the router to every non-internal entrypoint, so the host also answers unencrypted on `:80`. |
| 3 | `router.tls.certresolver`, **or** `ingress.zachd/tls-terminates-at-edge: "true"` | Otherwise Traefik falls back to its self-signed default certificate. |

## The one legitimate exemption

Hosts published through the Cloudflare tunnel terminate TLS at Cloudflare's
edge; the tunnel then dials Traefik with *No TLS Verify* on, so the certificate
Traefik presents is never validated. For those, naming a certresolver isn't
merely pointless — it would **fail**: DNS-01 can only answer for the
`duckdns.org` zone, and one failing domain can take down the whole ACME order,
wildcard included.

So they opt out by annotation rather than being quietly skipped:

```yaml
ingress.zachd/tls-terminates-at-edge: "true"
```

The distinction between *deliberately edge-terminated* and *someone forgot* is
the entire value of rule 3, so it has to be stated rather than inferred.
`web/talaria-deals` is currently the only user.

## Why VAP and not Kyverno

This is three CEL expressions. `ValidatingAdmissionPolicy` is built into the API
server (GA since 1.30; this cluster is on v1.34.3), so it costs no pods, no
webhook, no certificate rotation, and adds no new failure mode to the admission
path. A webhook-based policy engine that is down either blocks every write or
silently permits everything. A VAP cannot be down, because it *is* the API
server.

`failurePolicy: Ignore` for the same reason: if a rule is ever malformed, an
unevaluatable policy must not become an outage that blocks every Ingress write.
Drift is a slow problem; not being able to deploy is a fast one.

## Advisory first, enforcing later

Ships as `validationActions: [Warn, Audit]` — a non-conforming Ingress is still
admitted, but the client gets a warning and the API server writes an audit
annotation.

That's deliberate. Flipping straight to `Deny` would mean a foreign repo's next
routine deploy fails on a rule its authors have never read, and whatnowgg
redeploys every few hours.

Every Ingress in the cluster conforms as of this chart's first deploy, so the
wait is about catching what redeploys on a slower cycle, not about clearing a
backlog. To enforce, set in `values.yaml`:

```yaml
binding:
  validationActions:
    - Deny
```

`upgrade.sh` dry-runs the rules against every live Ingress *before* applying and
prints what would be rejected, so that flip is never a guess.

## Deploy

```bash
./upgrade.sh
```

It refuses to run if the API server doesn't serve VAP, reports the current
enforcement mode, and then proves the policy actually fires by submitting a
deliberately non-conforming Ingress with `--dry-run=server` — a real trip
through the admission chain, not a check that the object exists.

Check what's failing at any time:

```bash
kubectl get validatingadmissionpolicybinding ingress-policy \
  -o jsonpath='{.spec.validationActions}{"\n"}'

# warnings surface on the client that wrote the Ingress, so the reliable
# view is to re-run the pre-flight:
./upgrade.sh    # prints "N Ingresses checked" and any failures
```

## What it deliberately does not check

- **`spec.tls`.** Traefik's cert request is driven by the
  `router.tls.domains.0.*` annotations, not by `spec.tls`, so requiring the
  block would enforce tidiness rather than correctness. `web/apartment-watch` has
  the annotations and no `spec.tls` block, and works fine.
- **Hostname shape.** New `*.zachd.duckdns.org` hosts need no registration
  anywhere — DuckDNS resolves any label to the same A record and the wildcard
  cert already covers it. There is nothing to validate.
- **Anything in `kube-system`.** Those objects are created by k3s itself and
  answer to upstream defaults, not ours.
- **Non-HTTP ingress.** `mc-minecraft` (TCP 25565), `mc-minecraft-bluemap`
  (:8100 on every node IP), and `headlamp` (NodePort 30100) are LoadBalancer and
  NodePort services that bypass Traefik entirely. An Ingress policy cannot see
  them; they are listed in the repo README instead.
