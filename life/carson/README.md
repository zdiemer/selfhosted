# carson — Mr. Carson, the personal CRM

A single-user personal assistant on the cluster: a contact list with important
dates, a reminder ladder that texts before each one, a todo tracker, and a
calendar feed both Apple and Google subscribe to. Dashboard behind Authelia at
**`carson.zachd.duckdns.org`**; reminders arrive as plain SMS through
[`infra/sms-relay`](../../infra/sms-relay/).

Named for the Downton Abbey butler — the keeper of the household who notices
everything and disapproves of unanswered correspondence. `carson` is the
machine name; **Mr. Carson** is the voice the SMS is written in, because
"Sir — Rachel's birthday is tomorrow. A card, at the very least, would not go
amiss." gets read where a notification gets swiped away.

> **This is phase 1 of [`plans/carson.md`](../../plans/carson.md).** Contacts
> and dates are entered by hand. Email, iMessage, calendar ingestion and the
> Ollama/Claude tiers are phases 2–6 and are **not built**. The plan file is
> still the design of record; this README covers what actually runs.

---

## Architecture

One image, one container, three entrypoints (`src/main.py`):

```
                    ┌──────────────────────────────┐
  Authelia ────────▶│ /            dashboard (UI)  │
  (ingress)         │ /ui/*        form posts      │
                    ├──────────────────────────────┤
  Apple/Google ────▶│ /feed/<tok>.ics   NO AUTH    │──▶ birthdays, events, due dates
  (2nd ingress)     ├──────────────────────────────┤
                    │ /api/v1/*    JSON, in-cluster│◀── Claude runs (phase 5)
  ClusterIP ───────▶│ /healthz                     │
                    └───────────────┬──────────────┘
                                    │
                            SQLite on a PVC
                                    │
                    ┌───────────────▼──────────────┐
   08:00 daily ────▶│ CronJob: `remind`            │──▶ sms-relay ──▶ handset
                    │ T-21 / T-7 / T-1 / day-of    │
                    └──────────────────────────────┘
```

`serve` is a Deployment, `remind` is a CronJob, and that split is deliberate: a
reminder loop running as a background thread inside a long-lived pod can die
quietly and nobody finds out until a birthday is missed. As a CronJob it either
ran or it did not, and `kubectl get jobs` says which.

**The CronJob does not open the database.** It POSTs to
`/api/v1/reminders/run` on the ClusterIP service and the web pod does the work.
That is not indirection for its own sake — it was a bug on the first deploy.
The PVC is ReadWriteOnce, so a second pod mounting it only starts when the
scheduler happens to place it on the same node as the web pod; when it does
not, the job sits in `ContainerCreating` **forever** and the reminder is
silently never sent. Observed exactly that: job on node-4, volume attached to
node-5. Driving it over HTTP removes the node coupling and leaves exactly one
SQLite writer. The Job's exit status still carries the signal — a failed call
is a failed Job.

## Two Ingresses, and why

This is the one piece of the chart that looks like duplication and is not.

Traefik attaches middleware to a **router**, and one Ingress is one router per
host — so there is no way to express "Authelia on `/`, none on `/feed`" in a
single object. Hence `ingress.yaml` (gated) and `ingress-feed.yaml` (not).

The feed *cannot* sit behind Authelia. Apple Calendar and Google Calendar
cannot complete an interactive login; put the feed behind one and they do not
report an error, they simply stop refreshing — and the symptom surfaces weeks
later as "carson stopped adding birthdays". What protects it instead is
`secrets.feedToken`, 32 bytes of urlsafe randomness in the path, compared with
`secrets.compare_digest` and answering **404** (not 403) on a mismatch so that
probing cannot even confirm the endpoint exists.

The feed Ingress also carries `ingress.zachd/public-unauthenticated: "true"`.
That is not a way to silence [`infra/ingress-policy`](../../infra/ingress-policy/) —
it is the point of it. An un-gated Ingress has to *say* it is deliberate, so
that a forgotten one and an intentional one do not look identical in `kubectl`.

Consequence worth knowing before you rotate it: changing `feedToken` silently
breaks every subscription until each device re-adds the URL.

## Why the calendar is read-none/write-own

carson never writes to iCloud or Google. It publishes one feed and both
subscribe. Every device shows the events natively, no write credential exists
anywhere, and unsubscribing removes carson cleanly.

The cost is refresh latency — Google polls in hours, Apple 15 minutes at best.
That is why SMS exists alongside it: **the feed carries planned things**
(birthdays, anniversaries, confirmed events, due dates) while **anything
time-sensitive rides a text**.

## The reminder ladder

`src/reminders.py`, evaluated once a day:

| Stage | Message |
|---|---|
| **T−21** | The occasion, the date, the age. Gift ideas if the Claude tier has researched any (phase 5); otherwise a note that it will. |
| **T−7** | The shortlist, and "order by Wednesday if it is to arrive in time". |
| **T−1** | "A card, at the very least, would not go amiss." |
| **day-of** | "Do reach out." |

Deciding *that* a reminder is due is arithmetic on a date, so no model is
involved — if Ollama and Claude are both down this still texts on time. The
Claude tier's only contribution is the gift research stapled into T−21/T−7.

**Idempotency** is `reminder_log`'s `UNIQUE (important_date_id, year, stage)`,
claimed *before* the send and released if the send fails. Claiming first means
two concurrent sweeps cannot double-text; releasing on failure means a relay
outage doesn't consume the reminder permanently.

**There is no catch-up, on purpose.** If the pod was down through the T−7
window, T−7 is gone and T−1 still fires. "This was a week away, a week ago" is
worse than silence.

Leap-day birthdays are observed on **Mar 1** in common years, not Feb 28 — the
reminder should not arrive the day before everyone else's wishes do.

## Secrets

`op://homelab/life-carson/values.local.yaml`, resolved into memory by
`upgrade.sh` via [`scripts/lib/secret-values.sh`](../../scripts/lib/secret-values.sh).
Nothing is written to disk. See [`values.local.yaml.example`](values.local.yaml.example).

| Key | What it is |
|---|---|
| `secrets.smsApiKey` | Per-service key from sms-relay's `SMS_RELAY_API_KEYS`. Add `carson` there first. |
| `secrets.feedToken` | The unguessable path segment in the feed URL. The only thing protecting it. |
| `sms.to` | Where reminders go, E.164. |

The chart **fails to render** if any is missing, rather than deploying a
half-configured release that composes reminders and sends them nowhere.

## Operating

```bash
./build.sh      # image -> ghcr.io/zdiemer/carson (public package)
./upgrade.sh    # helm upgrade --install into namespace `life`

# Run a reminder sweep now instead of waiting for 08:00:
kubectl -n life create job --from=cronjob/carson-reminders carson-manual-$(date +%s)

# Aim a sweep at a date that actually has something on it, without inventing
# a contact whose birthday is genuinely today:
./upgrade.sh --set extraEnv.CARSON_TODAY=2026-09-03
```

Nothing due means no text, by design. The log line is
`reminder sweep complete: N sent`.

### First deploy

1. Add `carson` to sms-relay's `SMS_RELAY_API_KEYS`, take the key it issues.
2. `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` for the feed token.
3. Put both, plus the recipient number, in `op://homelab/life-carson/values.local.yaml`.
4. `./build.sh` then set the GHCR package to **Public** (multi-node cluster,
   every node pulls anonymously).
5. `./upgrade.sh`.
6. **Subscribe the feed in both calendars** — it is the only way the dates reach
   a device. `upgrade.sh` elides the token by default (it is a live credential
   and scrollback is not a safe sink); print it with
   `CARSON_SHOW_FEED_URL=1 ./upgrade.sh`, or
   `./scripts/secrets.sh show life/carson --reveal`:
   - iOS: Calendar → Calendars → Add Calendar → Add Subscription Calendar
   - Google: Other calendars → + → From URL
7. Enter the ~30 people who matter at `/ui/people`. Hand-entering is the
   curation, not a chore to automate away.

## Data

One SQLite file at `/data/carson.db` on a `truenas-iscsi` PVC annotated
`helm.sh/resource-policy: keep`. Everything in it is hand-entered, not derived —
losing it is not a resync, it is retyping. Only the web pod mounts it (see
above), so there is exactly one writer; WAL mode still earns its place for
concurrent readers within that pod.

Fourteen tables (`src/db.py`). The ones that matter now are `person`, `handle`,
`important_date`, `todo`, `note` and `reminder_log`; the rest — `proposal`,
`event`, `escalation`, `source_cursor`, `interaction`, `unmatched_handle`,
`gift_idea`, `gift_history` — exist so phases 2–6 do not need a migration.

Two schema decisions worth not undoing:

- **`handle` is separate from `person`** because handle→person is the spine of
  the whole design: an email address, a phone number and an iMessage id all have
  to land on one contact. Unmatched handles are never guessed at — they
  accumulate in `unmatched_handle` for a human to triage.
- **`important_date.year` is nullable.** Half the birthdays anyone knows come
  without a year, and a schema that demands one invites a fake.

## Not built yet

Phases 2–6 of [`plans/carson.md`](../../plans/carson.md): calendar ingest
(iCloud CalDAV + Google secret ICS), email over IMAP, iMessage via
plug-in-triggered backups on zachd-ubuntu, the Ollama extraction queue, the
SMS proposal round-trip, and the gateway-scheduled Claude runs (gift research,
morning digest, escalation drain).

The iMessage path in phase 4 was **proven end-to-end on 2026-08-25** but is not
implemented — see the plan for the measured costs, which are steep
(~17 min and ~800 MB per sync, with no incremental discount).
