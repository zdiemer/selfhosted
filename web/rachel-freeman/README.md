# rachel-freeman

An online art store for original paintings and prints, self-hosted on this
cluster. Live at **https://rachelfreeman.art** through the shared Cloudflare
tunnel, and at **https://rachelfreeman.zachd.duckdns.org** on the DuckDNS
ingress — the second one is a way in that does not depend on Cloudflare.

Source lives in [zdiemer/rachelfreeman](https://github.com/zdiemer/rachelfreeman)
(`~/code/rachelfreeman`); this directory is the chart.

```
                     ┌──────────────────────────────┐
   Rachel  ──/admin──▶                              │
                     │   one image, two halves      │──▶ Postgres (catalogue,
   buyers  ──────────▶   Payload CMS + Next.js      │       orders)
                     │                              │──▶ media PVC (photos)
                     └───────────┬──────────────────┘
                                 │ price + stock read server-side
                                 ▼
                         Stripe Checkout (hosted)
                                 │ checkout.session.completed
                                 ▼
                      /stripe/webhook → order row, stock down
```

## The one design decision

**Prices live in Postgres, not in Stripe.** The checkout route reads the price,
the stock and the shipping rate off the artwork document at the moment it creates
a session, and hands Stripe an amount. There is no product catalogue on the
Stripe side, so there is nothing to keep in sync and no way for the two to
disagree. Stripe is a payment rail, not a second admin panel.

That is what makes this maintainable by someone non-technical: Rachel adds a
piece, uploads photos, sets a price and ticks Published, in one place, and the
shop is correct.

**The webhook is what marks something sold**, not the Buy button — it is the only
signal money actually moved. A buyer who reaches Stripe and abandons must not
take a painting off sale. Originals are forced to stock 1 and sell exactly once.

## Deploying

```bash
./build.sh     # only when the app source changed
./upgrade.sh   # chart + values.local.yaml → the cluster
```

`build.sh` builds on the in-cluster buildkitd and pushes to GHCR. **Do not build
in the claude-workspace pod's shell** — `next build` wants ~4Gi and that cgroup
is shared with the messaging gateway; doing it there OOM-killed the gateway twice
on 2026-08-15.

Bump `image.tag` in `values.yaml` (and `appVersion` in `Chart.yaml`) for every
rebuild. `pullPolicy: IfNotPresent` means a re-push under the *same* tag is
silently ignored by any node that already cached it — which presents as code
changes having no effect, and cost a confused round trip the first time.

### The GHCR package is private

Unlike every other chart here, which uses a public package pulled anonymously.
Nothing in the image is secret — credentials all arrive as env at runtime — but
publishing someone else's storefront source is not a default worth taking. So
`imageCredentials.pat` (read:packages) is **required** in `values.local.yaml`,
and `templates/imagepullsecret.yaml` renders the pull secret.

## Secrets

From 1Password, as everywhere else (`op://homelab/web-rachel-freeman`):

| Key | Required | What |
|---|---|---|
| `app.payloadSecret` | yes | signs admin login cookies |
| `postgres.password` | yes | the database |
| `imageCredentials.pat` | yes | pulls the private GHCR image |
| `app.stripeSecretKey` | **no** | Stripe API key (test key until go-live) |
| `app.stripeWebhookSecret` | **no** | signing secret of the webhook endpoint |

The Stripe pair is deliberately optional. Without it the gallery, the admin
panel and uploads all work and the Buy button answers *"payments are not set up
yet"* — so the store can be built and filled with artwork before the payment
account exists. Nothing can be marked sold until the webhook secret is set,
because the webhook is the only thing that marks a sale.

```bash
secrets edit web/rachel-freeman     # to add the Stripe keys later
```

## Still to do

1. **Stripe.** The only thing standing between this and taking money. Create the
   account, add the two secrets above, and point a webhook endpoint at
   `https://rachelfreeman.art/stripe/webhook` subscribed to
   `checkout.session.completed`.
2. **`www.rachelfreeman.art`** does not resolve — only the apex has a public
   hostname on the tunnel. Add one in the Cloudflare dashboard and append the
   name to `ingress.cloudflareHosts`, or leave it if the apex is the only name
   anyone is given.

Done: the admin account exists (so create-first-user is closed), and the domain
is live on the tunnel.

## Backups

The `web` namespace already had a k8up schedule (Saturdays 01:00), so no schedule
change was needed. What was added is a **PreBackupPod** in `infra/k8up`
(`rachel-freeman-postgres-dump`) — a `pg_dump -F c` taken over the service, which
is the restore path. See the long note at the bottom of
`infra/k8up/templates/prebackuppods.yaml` for why the dump and not the volume
snapshot is the thing to trust.

The **media PVC is the half that matters most**: a database can be rebuilt from a
dump, a photograph of a sold painting cannot.

## Schema changes

Payload's `push` is a development-only path — the adapter gates it on
`NODE_ENV !== 'production'`. In the cluster the schema comes from
`prodMigrations`, imported statically in `payload.config.ts` so Next traces them
into the standalone bundle and the container needs no CLI.

After changing a collection:

```bash
kubectl -n web port-forward svc/rachel-freeman-postgres 55432:5432 &
DATABASE_URL=postgres://payload:<pw>@localhost:55432/rachelfreeman \
  npm run payload -- migrate:create <name>
```

and commit the result. Skipping this is how you get an app that serves `/admin`
and `/healthz` with a 200 while every storefront page 500s on
`relation ... does not exist`.
