# carson — personal CRM / assistant ("Mr. Carson")

**Mostly a plan.** Phase 1 now exists as a chart at [`life/carson`](../life/carson/)
— written 2026-08-25, lints clean, runs locally, **not yet deployed** (that
needs the 1Password item and a `./upgrade.sh`). Phases 2–6 below are still
design only. Durable phase-1 reasoning has moved into that chart's README, per
the convention in [`README.md`](README.md); what stays here is everything not
yet built.

This is the design for a personal assistant app — todo tracker, event
reminders (birthdays/anniversaries with researched gift ideas), calendar
assistant, and email/iMessage watcher — that lives on the cluster and talks
to Zach through the surfaces that already exist (SMS via `infra/sms-relay`,
a published calendar feed, and Signal — which stays what it already is, a
Claude conversation surface, not a notification channel).

Status: **phase 1 built, not deployed; phases 2–6 not started.**

- **Phase 1** — chart, SQLite, REST API, Authelia dashboard, published ICS
  feed, SMS reminder ladder: written 2026-08-25 at [`life/carson`](../life/carson/).
  Needs the `op://homelab/life-carson` item created, then `build.sh` +
  `upgrade.sh`.
- **Phase 4** (iMessage) — feasibility proven end-to-end against the real phone
  on 2026-08-25 and the pairing is done, but nothing is implemented. That work
  invalidated four of this plan's original assumptions; see the iMessage
  section for what was measured and what it cost.
- Phases 2, 3, 5, 6 — untouched.

Named for the Downton Abbey butler: the keeper of the household who notices
everything and disapproves of unanswered correspondence. `carson` is the
machine name (chart, namespace, subdomain, `op://` item); **Mr. Carson** is
the display name, and the voice the SMS output should be written in —
reminders land better as "Sir, Rachel's birthday is Thursday and you have
not yet acquired a card" than as a notification.

It lives in this repo because everything it touches does: Ollama (`ai/`),
the messaging gateway (`dev/claude-workspace`), sms-relay (`infra/sms-relay`),
Authelia
(`auth/`), the NAS-backed PVCs (`infra/democratic-csi`), and the
1Password-backed secret pattern (`dist/secrets`, `scripts/lib/secret-values.sh`).

## Why

The failure mode of every personal CRM / todo system is that logging is a
chore, so the data goes stale and the tool gets abandoned. The design bet
here is the opposite: **ingestion is passive** (email, calendars, iMessage
flow in on their own), **capture is conversational** (text the bot), and
**output rides channels already in daily use** (plain SMS, the calendar app). Nothing requires opening a new app to stay current.

Four jobs, one data store:

1. **Todo tracker** — commitments extracted from email/iMessage ("I'll send
   you the doc"), manual capture over Signal ("todo: renew passport"), and
   CRM-generated follow-ups. Every extracted todo keeps a link to its source
   (person + message snippet), so a nag can say *"you told Rachel you'd send
   the photos — 5 days ago."*
2. **Event reminders** — birthdays and anniversaries on contact records, with
   a lead-time ladder (see below) and **researched gift ideas with real
   links**, produced by a scheduled Claude run with web search.
3. **Calendar assistant** — reads both calendars, notices events mentioned in
   messages that are *not* on any calendar, and proposes them. Never writes
   to iCloud or Google; publishes its own feed both subscribe to.
4. **Email/message watcher** — the ingestion layer feeding 1–3, plus
   last-contacted freshness for every known person, which drives
   "you're drifting from X" nudges.

## Architecture

One app chart at [`life/carson`](../life/carson/) — a new top-level `life/`
group, decided 2026-08-25 — following the apartment-watch/money shape: Python app + SQLite on a PVC, image built by `build.sh`
to GHCR, `values.local.yaml` from 1Password via `sv_load`, Authelia-gated
duckdns host for the web UI.

```
 sources                      carson pod                       outputs
 ───────                      ───────────                       ───────
 Gmail (IMAP idle) ─────┐    ┌──────────────────┐    ┌─ feed.ics (secret URL;
 iCloud (CalDAV ro) ────┼───▶│ ingest workers    │    │   Apple+Google subscribe)
 Google cal (secret ICS)┘    │ normalizer        │───▶├─ SMS via sms-relay
 iMessage (on plug-in,  ────▶│ extraction queue  │    │   (reminders, proposals;
   pymobiledevice3, below)   │ SQLite on PVC     │    │    replies via webhook)
 sms-relay inbound ─────────▶│                   │    ├─ web UI (Authelia)
                             └────────┬─────────┘    └─ REST API (for Claude runs)
                                      │ escalation table
                             ┌────────▼─────────┐
                             │ Ollama (easy)     │  ollama.ai.svc:11434
                             │ Claude (hard)     │  gateway schedules → Signal
                             └──────────────────┘
```

### Two-tier LLM routing

- **Ollama** (`ollama.ai.svc.cluster.local:11434`) handles the volume work:
  classify each new email/message (personal / transactional / noise), extract
  candidate events, dates, commitments. Constraint that shapes the whole
  pipeline: the deployment is CPU, one resident 8B model, **one request at a
  time** (`OLLAMA_NUM_PARALLEL=1`). So extraction is a serial queue, batched
  after each ingest sweep — not per-message real-time. Fine: nothing here
  needs sub-minute latency.
- **Claude** handles judgment work via `messaging.schedules` entries in the
  gateway (the durable cron that survived where ScheduleWakeup chains died —
  see dev/claude-workspace README "Schedules"): gift research, the weekly
  review, and draining an **escalation table** where Ollama parks anything it
  extracted with low confidence (ambiguous dates, multi-party plans,
  "is this actually a commitment?"). Claude runs reach carson through its
  REST API in-cluster and reply to Zach over the pinned Signal thread.

### Proposals: SMS round-trip, not silent writes

LLM extraction is good but not "silently write to my calendar" good. Every
extracted event or todo lands as a **proposal**, delivered as a plain text
message through `infra/sms-relay` (carson holds an API key, idempotency
keys make retries safe): *"Add: dinner w/ Sarah, Sat 6pm (from iMessage).
Reply Y1 / N1."* Carson subscribes to the relay's **inbound webhook** fan-out
and matches replies by short code — each pending proposal gets one, so
several can be in flight. Accepted events go onto the published ICS feed;
accepted todos into the tracker; anything else ("make it 7pm instead") is
kept as a free-text reply on the proposal for the nightly Claude run to
resolve. Conversational back-and-forth beyond that happens over the Signal
gateway, which stays a Claude surface only — carson never notifies there.

### Calendar: read two, write none

- **iCloud**: CalDAV against `caldav.icloud.com` with an app-specific
  password (generated at appleid.apple.com, stored at `op://homelab/life-carson`).
  Python `caldav` handles principal discovery.
- **Google**: the calendar's **secret ICS address** ("Secret address in iCal
  format" in calendar settings) — read-only, no OAuth, no API project. Poll
  hourly.
- **Write path**: carson publishes `https://carson.zachd.duckdns.org/feed/<token>.ics`
  and both Apple Calendar and Google Calendar subscribe. Every device shows
  carson events natively; no write credentials exist anywhere; unsubscribing
  removes carson cleanly. Known limit: subscribed-feed refresh is slow
  (Google: hours; Apple: configurable, 15 min floor), so the feed carries
  *planned* items (birthdays, confirmed events, follow-up dates) while
  anything time-sensitive rides SMS.
- **Cross-reference**: extracted candidate events are matched against both
  calendars (fuzzy: date window + attendee/title tokens) before proposing, so
  already-scheduled things are never re-proposed.

### Email: IMAP, three privacy layers

Gmail over IMAP with an app password (2FA account, password in 1Password).
IMAP IDLE for near-real-time, full sweep on reconnect. Processing is tiered,
and the tier is enforced *before* content reaches any model:

1. **Metadata, everyone**: from/to/date headers update last-contacted for
   known people. No content read. This alone keeps the nudge engine honest.
2. **Transactional extraction, any sender**: Ollama classifies; confirmations
   (flights, reservations, appointments, invites) get parsed into candidate
   events → proposal flow.
3. **Personal enrichment, whitelisted contacts only**: full-body extraction of
   life updates and commitments, both directions ("I'll…" in sent mail is the
   todo goldmine). The whitelist is the CRM contact list with an explicit
   per-contact flag, default off.

### iMessage: plug-in-triggered USB backup

**Measured end-to-end on 2026-08-25** against the actual phone (iPhone13,4,
iOS 26.6) from zachd-ubuntu. The original design here was a nightly wireless
`idevicebackup2` CronJob; that **cannot work**, and what follows is what the
hardware actually permits.

**Tooling: `pymobiledevice3`, not `libimobiledevice`.** Ubuntu 24.04 ships
libimobiledevice 1.3.0 (2020), which does not reach modern iOS.
[`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3) is pure
Python, pip-installable, and paired/queried/backed up iOS 26.6 without
complaint. It also carries its own raw-socket mDNS browser and a
`lockdown wifi-connections` CLI — neither of which this design ends up needing.

**Pairing (once, manual).** `pymobiledevice3 lockdown pair` over USB with the
phone unlocked. The record lands at `~/.pymobiledevice3/<udid>.plist` — 9.4 KB,
user-owned, no root, no `/var/lib/lockdown`. It carries an `EscrowBag` and the
device's `WiFiMACAddress`. This is the *only* change the design makes to the
phone; backup encryption stays off, since `--only sms` gains nothing from it.

**Every backup needs a human, and that is not negotiable.** Since iOS 16.1 the
device requires its passcode typed *on the handset* to start a host backup —
observed twice here, so it is per-backup, not per-trust. There is no unattended
path: not over Wi-Fi, not over USB, not at 3am. (`lockdown pair-supervised`
bypasses it, but supervising a personal phone means erasing it and enrolling
through Apple Configurator on a Mac.) Hence the trigger is **plugging the phone
into zachd-ubuntu**, where a human is by definition present.

Locking *mid-transfer* is fine, though — `MBErrorDomain/208 Device locked` is a
handshake-time check only. Measured: the phone was locked 90s into a run and
~18k files kept flowing to completion. So the interaction is: plug in, tap the
passcode once, lock the phone, walk away.

**Cost per sync — the number that shapes everything:**

| | |
|---|---|
| full backup (`--only sms`) | 980 s, 986 MB on disk |
| second, "incremental" run | **1038 s — longer than the full backup** |
| `sms.db` re-transferred | 774 MB, every single run |

There is no incremental benefit. MobileBackup2 diffs at *file* granularity and
`sms.db` is one monolithic SQLite file that changes on every received message,
so it is always "changed" and always re-sent whole. Budget ~17 min and ~800 MB
per sync regardless of how little actually happened. This is the single most
expensive component in carson, and the reason phase 4 is last.

`--only sms` (`HomeDomain/Library/SMS/sms.db`) is worth using but saves **disk,
not time**: pymobiledevice3's filter is receive-and-discard — `device_link.py`
drains every non-matching file off the wire into a 0-byte placeholder — so a
filtered backup costs the same wall-clock as a full one. `--only messages`
additionally keeps `MediaDomain/Library/SMS/**`, i.e. every photo ever texted
(4.2 GB+ here), for no analytical gain.

**Extraction.** [imessage-exporter](https://github.com/ReagentX/imessage-exporter)
4.2.0 reads the backup directly (`--platform iOS --db-path <root>`) and yielded
1,243 threads / ~680k messages / 51 MB of text spanning 2018–2025. Two gotchas
worth remembering: the backup dir must be mounted **writable** (SQLite needs
its sidecars; a read-only mount dies with "unable to open database file"), and
`--only sms` omits `AddressBook.sqlitedb`, so contact names come out
unresolved — carson does its own handle→person matching anyway.

For the actual pipeline, read `sms.db` directly with SQL against a `ROWID`
high-water mark. imessage-exporter re-exports all 680k messages every run; keep
it as a verification/fallback tool, not the hot path.

**Runner: zachd-ubuntu, not the cluster.** A human has to be at the machine the
phone plugs into, which settles the open question below. Follows the existing
`scripts/systemd/` pattern — a user timer polling `pymobiledevice3 usbmux list`
is simpler than bridging udev's system units into a user session — with
`OnFailure=selfhosted-alert@%n.service` as the staleness alarm. Backups land on
an NFS mount rather than a PVC. pymobiledevice3 logs `WARNING Please enter the
device passcode to continue the backup`, which the runner can watch for to text
a nudge rather than hang silently.

- **Same three privacy tiers as email.** Group chats default to
  metadata-only, or the todo inbox fills with other people's plans. The
  sms-relay handset's number is excluded entirely — otherwise the backup
  feeds carson its own reminders back as "extracted events."
- **Health check**: iOS updates occasionally invalidate pairing. A failed run,
  or a backup older than a week, must surface in the morning digest — a
  silently stale iMessage feed defeats the whole design. Note the cadence is
  now "whenever Zach plugs in", not nightly, so the staleness threshold has to
  be generous enough not to cry wolf over a busy week.

### Birthdays, anniversaries, gifts

Contact records carry important dates. The reminder ladder per date:

- **T−21 days**: a gateway-scheduled Claude run wakes for any date inside the
  window, reads that person's carson notes (interests, recent life updates,
  past gifts given — carson records what was gifted each year to avoid
  repeats), does real web research, and texts (via sms-relay) 3–5 concrete
  gift ideas **with live product links** and prices, plus a card suggestion.
  Ideas are also stored on the contact so the T−7 reminder can reference them.
- **T−7**: SMS reminder with the shortlist ("order by ~Wed for shipping").
- **T−1 and day-of**: reminder to reach out; the date itself is on the ICS
  feed year-round.

This is the clearest "hard tier" case: it needs web search, judgment, and
taste — Claude via the gateway — while the ladder scheduling itself is
deterministic app code.

### Nudge engine

Per-contact cadence tier (close ~2w, family ~1w, keep-in-touch ~2mo, none).
Last-contacted is fed passively by email + iMessage metadata (any direction).
A drifting contact surfaces in the **morning digest** — one scheduled Claude
run that composes: overdue todos (with source snippets), today/tomorrow
calendar across both feeds, pending proposals, drifting contacts with their
last note, upcoming dates — and delivers it by POSTing to sms-relay (the
run's prompt ends "send via sms-relay and reply with no text", so nothing
leaks onto the Signal thread). Quiet when there's nothing. SMS is the format
constraint that keeps the digest honest: a few concise segments, not a page.

## Data model (SQLite)

- `person` (name, aliases/handles [email addrs, phone numbers, iMessage ids],
  cadence_tier, enrich_optin, notes)
- `important_date` (person, kind [birthday/anniversary/custom], date,
  gift_history)
- `interaction` (person, ts, channel [email/imessage/calendar/manual],
  direction, source_ref) — feeds last-contacted
- `note` (person, ts, text, source_ref)
- `todo` (text, status, due, person?, source_snippet, source_ref,
  origin [extracted/manual/generated], nag_state)
- `proposal` (kind [event/todo], payload, confidence, status
  [pending/accepted/ignored], source_ref)
- `event` (accepted events + generated reminders → rendered into feed.ics)
- `escalation` (raw item, why Ollama punted, status) — drained by the nightly
  Claude run
- `source_cursor` (per-source high-water marks: IMAP UID, iMessage ROWID,
  calendar etags)

Handle→person matching is the load-bearing join (email addr, phone, iMessage
handle all map to one person); unmatched handles accumulate in a triage list
in the web UI rather than guessing.

## Secrets (`op://homelab/life-carson`)

Gmail app password; iCloud app-specific password (CalDAV); ICS feed token;
sms-relay API key (and the webhook signing secret for inbound). No iPhone
backup password is needed — backup encryption stays off (see iMessage above) —
and the pairing record stays a local file on zachd-ubuntu
(`~/.pymobiledevice3/<udid>.plist`) rather than a k8s Secret, since the backup
runs on that machine and never in-cluster. Ollama
basic-auth is unnecessary in-cluster (ClusterIP bypasses the Traefik gate —
the auth exists for the public host).

## Order of work

Each phase is independently useful; stop anywhere and it still earns its keep.

1. ~~**Core + capture + dates**~~ — **BUILT 2026-08-25**, see
   [`life/carson`](../life/carson/). Chart, SQLite on a PVC, JSON API, web UI
   behind Authelia, feed.ics on a second un-gated Ingress, and the T-21/-7/-1/
   day-of reminder ladder as a CronJob through sms-relay. Not yet deployed: it
   needs the `op://homelab/life-carson` vault item, then `build.sh` +
   `upgrade.sh`, then the feed subscribed on both calendars. Already a birthday
   tracker + todo list the day it lands.
2. **Calendar ingest**: iCloud CalDAV + Google secret ICS pollers;
   cross-reference; digest gains "today across both calendars."
3. **Email**: IMAP worker, metadata tier (last-contacted + nudges), then
   Ollama transactional extraction → SMS proposal round-trip (send +
   inbound-webhook replies).
4. **iMessage**: pairing (done), a plug-in-triggered backup runner on
   zachd-ubuntu under `scripts/systemd/`, and an `sms.db` → `ROWID` delta
   pipeline into the same extraction queue. Feasibility proven end-to-end
   2026-08-25; the ~17 min / ~800 MB per-sync cost is why this stays late in
   the order.
5. **Claude tier**: gateway schedule entries — nightly escalation drain,
   morning digest, gift-research ladder.
6. **Enrichment tier**: whitelisted full-content extraction (commitment/todo
   mining from sent mail + iMessage).

## Open questions

- ~~**Where does the phone sleep?**~~ and ~~**hostNetwork CronJob vs.
  zachd-Ubuntu**~~ — both **settled 2026-08-25** by the measurements in the
  iMessage section. iOS requires an on-device passcode tap per backup, so the
  runner has to be wherever a human is: zachd-ubuntu, over USB, triggered by
  plugging in. No cluster CronJob, no mDNS, no Wi-Fi sync, and the
  network-segmentation VLAN split no longer affects this at all.
- **Is iMessage ingestion worth ~17 min and ~800 MB per sync?** The measured
  cost has no incremental discount and never will (monolithic `sms.db`). Email
  (phase 3) covers commitments and last-contacted for anyone who mails; the
  question is how much of the CRM's value lives *only* in iMessage. Worth
  answering before building phase 4 rather than during — the answer may be
  "sync weekly, not daily", or "skip it".
- **Contact seeding**: hand-enter the ~30 people who matter, or import from
  CardDAV/Google Contacts once at setup? (Lean: hand-enter; the list *is*
  the curation.)
- ~~**Group name**~~ — settled 2026-08-25: a new top-level `life/` group,
  future home for health tracking and the like. `web/` is sites and web apps;
  carson is an ingestion daemon that happens to have a UI.
