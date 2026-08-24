# guacamole — desktops in a browser

Apache Guacamole: `guacd` speaks RDP to a guest, the web app renders it to an
HTML5 canvas over a WebSocket. A real Windows desktop at a URL, with a shared
clipboard and a resolution that follows the browser window. No client installed,
nothing to configure on the viewing device.

Live at **https://windows.zachd.duckdns.org** — tailnet only (see below).

## Use it alongside the KubeVirt console, not instead of it

| | when |
|---|---|
| `virtctl vnc` | install-time, pre-boot, firmware, recovery — anything where Windows itself isn't up to answer RDP |
| Guacamole | daily use, once Windows is running: clipboard, audio, dynamic resolution |

## Why it's VPN-only for free

`infra/duckdns` runs in **`tailnet` mode**, so every `*.zachd.duckdns.org` name
resolves to a cluster node's `100.x` Tailscale address. That is CGNAT space —
unroutable from the public internet. This host therefore answers only to devices
on the tailnet, with no IP allowlist, no port forward, and no extra moving
parts. TLS is the wildcard from `infra/traefik-certs`, served as Traefik's
default certificate.

`ingress.cloudflareHosts` is deliberately empty: adding a host there would
publish a remote desktop gateway on the public internet. If you ever do, turn
`auth.forwardAuth.enabled` on in the same change to put Authelia in front.

## Auth

Guacamole's built-in file provider (`user-mapping.xml`), rendered from
`values.yaml` into one Secret. No database — a stock Guacamole wants Postgres or
MySQL to hold two rows, which is a whole extra stateful service to back up and
upgrade. The trade is that connections are declared in git rather than edited in
the UI; for a fixed set of VMs that's an improvement.

Authelia forward-auth is **on**, in front of Guacamole's own login.
`infra/ingress-policy`'s authPolicy asks every Ingress to state its position —
name a forward-auth middleware, or annotate
`ingress.zachd/public-unauthenticated="true"`. The second would be a false
statement here: the host is neither public (tailnet-only) nor unauthenticated
(Guacamole has a login), and an annotation that misdescribes a host is worse
than the warning it silences. It also costs almost nothing — Authelia's session
cookie is shared across `*.zachd.duckdns.org`, so you are usually already signed
in when you land here. Turn it off with `auth.forwardAuth.enabled: false` if you
disagree.

The guest credentials in each connection are **empty by default**, so Guacamole
shows Windows' own login screen in the browser and the Windows password never
has to be stored in the cluster at all. Fill them in `values.local.yaml` only if
you want one-click sign-in.

## Install

```bash
cp values.local.yaml.example values.local.yaml   # set auth.password
./upgrade.sh
```

Secrets come from `op://homelab/dev-guacamole` via `scripts/lib/secret-values.sh`
and are never written to disk.

## When the desktop doesn't load

Guacamole comes up healthy whether or not anything is on the other end, so a
green rollout says nothing about whether a desktop will render. In order of
likelihood:

1. **The VM is stopped.** `upgrade.sh` checks this and prints
   `NO ENDPOINTS` for any `*-rdp` Service with none. `virtctl start win11 -n dev`.
2. **Remote Desktop is off inside Windows.** Endpoints exist, connection
   refused. See `dev/win11/README.md` §Turn on RDP in the guest.
3. **Certificate error.** `ignore-cert: "true"` is already set — Windows'
   self-signed RDP certificate otherwise fails validation and Guacamole reports
   a bare "connection closed".

## Notes on the config

**`WEBAPP_CONTEXT=ROOT` is load-bearing.** The image's entrypoint installs the
war as `$CATALINA_BASE/webapps/${WEBAPP_CONTEXT:-guacamole}.war`, so out of the
box the app lives at `/guacamole` and Tomcat serves its own 404 page at `/` —
which is what this Ingress routes. Without the override the host answers every
request with *"HTTP Status 404 – Not Found ... Apache Tomcat/9.0.106"*.

The reason that is worth a paragraph is how well it hides. The probes must
point at the same path the war is deployed to; when they pointed at
`/guacamole/` while the Ingress served `/`, the pod reported 2/2 healthy, the
rollout went green, and the site was broken the whole time. An external check
does not catch it either if Authelia is in front — the forward-auth redirect
answers 200 long before anything reaches Tomcat. Probe `/`, and verify against
the pod directly.

**`guacd` is a sidecar, not its own Deployment.** The web app reaches it over a
plain unauthenticated TCP socket; keeping that inside the pod's network
namespace means it is never reachable from the cluster network and needs no
NetworkPolicy.

**One replica, `Recreate`, no PDB.** Guacamole keeps session state in the
servlet container, so a second pod wouldn't share sessions — it would just be a
coin flip over which one your WebSocket lands on. And a `minAvailable` budget
over a single replica blocks node drains forever, which
`scripts/ci-lint-availability.sh` enforces against.

**`security: any`.** Stricter than `nla` would be better, but NLA needs the
credentials up front — which defeats the empty-username default, since NLA has
nowhere to prompt.
