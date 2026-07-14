# kelsey-green

Serves [kelsey.green](https://kelsey.green) — a static Astro site for Kelsey B.
Green. Source: [zdiemer/kelsey-dot-green](https://github.com/zdiemer/kelsey-dot-green)
(private).

Unlike everything else in this repo, **there is no image to build**. The site is
built by GitHub Actions and pulled into the cluster:

```
Kelsey pushes to main
  → Actions runs `npm run build`, force-pushes dist/ to the `deploy` branch
  → the git-sync sidecar polls `deploy` every 60s and repoints a symlink
  → nginx serves it
  → cloudflared → Cloudflare edge → https://kelsey.green
```

The cluster **pulls**; GitHub never gets cluster credentials. The deploy key is
read-only, so a compromise of the cluster cannot push to Kelsey's repo. The
tunnel is outbound-only, so no port is forwarded and the home IP can change
freely.

Rollback is `git revert` on `main` in the site repo, and is live ~2 minutes
later. Nothing here needs to be touched to ship content.

## Deploy

```bash
cp values.local.yaml.example values.local.yaml   # add the deploy key
./upgrade.sh
```

`upgrade.sh` only needs running when the *chart* changes.

## Exposure

Two independent paths, both pointing at the same pods:

| Host | Path | Notes |
| --- | --- | --- |
| `kelsey.zachd.duckdns.org` | Traefik ingress | Rides the existing `*.zachd.duckdns.org` wildcard cert, like every other service here. Always on, and the way to verify the stack without Cloudflare. |
| `kelsey.green` | cloudflared tunnel | `cloudflared.enabled=true` + a token in `values.local.yaml`. Off until the Cloudflare zone exists. |

The tunnel is entirely disjoint from DuckDNS: it adds no DNS record, forwards no
port, and does not go through Traefik. Enabling it changes nothing about the
existing DuckDNS subdomains.

## Verify

```bash
kubectl -n web get pods -l app.kubernetes.io/instance=kelsey-green   # 2/2 per pod
kubectl -n web logs deploy/kelsey-green -c git-sync --tail=5
kubectl -n web port-forward svc/kelsey-green 8080:80 && curl -s localhost:8080 | head
```

A pod is only Ready once git-sync has produced a real page (readiness probes
`/`), so a pod that never syncs never takes traffic. Liveness probes a static
`/healthz` instead, so a broken sync keeps serving the last good version rather
than restarting in a loop.
