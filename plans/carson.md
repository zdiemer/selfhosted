# carson — personal CRM / assistant ("Mr. Carson")

**A plan, not a deployment.** Nothing here is installed and there is no chart.
This is the design for a personal assistant app — todo tracker, event
reminders (birthdays/anniversaries with researched gift ideas), calendar
assistant, and email/iMessage watcher — that lives on the cluster and talks
to Zach through the surfaces that already exist (SMS via `infra/sms-relay`,
a published calendar feed, and Signal — which stays what it already is, a
Claude conversation surface, not a notification channel).

Status: **not started.**

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

One app chart (`web/carson` or a new `life/` group), following the
gamedex/money shape: Python app + SQLite on a PVC, image built by `build.sh`
to GHCR, `values.local.yaml` from 1Password via `sv_load`, Authelia-gated
duckdns host for the web UI.

```
 sources                      carson pod                       outputs
 ───────                      ───────────                       ───────
 Gmail (IMAP idle) ─────┐    ┌──────────────────┐    ┌─ feed.ics (secret URL;
 iCloud (CalDAV ro) ────┼───▶│ ingest workers    │    │   Apple+Google subscribe)
 Google cal (secret ICS)┘    │ normalizer        │───▶├─ SMS via sms-relay
 iMessage (nightly      ────▶│ extraction queue  │    │   (reminders, proposals;
   idevicebackup2, see below)│ SQLite on PVC     │    │    replies via webhook)
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
  password (generated at appleid.apple.com, stored at `op://homelab/carson`).
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

### iMessage: nightly wireless backup (Option A)

`libimobiledevice` on Linux, no Mac required:

- **Pairing (once, manual)**: pair the iPhone over USB on zachd-Ubuntu
  (`idevicepair pair`, then enable Wi-Fi sync). The pairing record
  (`/var/lib/lockdown/<udid>.plist`) is then copied into a k8s Secret so the
  cluster job can use it without ever having touched the phone.
- **Nightly job**: a CronJob (hostNetwork, so mDNS/`_apple-mobdev2._tcp`
  discovery of the phone on the flat LAN works) runs
  `idevicebackup2 backup --network` to a NAS-backed PVC. Backups are
  incremental after the first; the first is slow (tens of GB) and should run
  overnight on wall power.
- **Extraction**: [imessage-exporter](https://github.com/ReagentX/imessage-exporter)
  reads the iOS backup format directly, including **encrypted** backups given
  the backup password (also in 1Password — encrypted stays on, it's what iOS
  wants and it covers more data classes). Carson diffs against a
  high-water-mark (last seen message ROWID/date) and feeds only the delta to
  the extraction queue.
- **Same three privacy tiers as email.** Group chats default to
  metadata-only, or the todo inbox fills with other people's plans. The
  sms-relay handset's number is excluded entirely — otherwise the backup
  feeds carson its own reminders back as "extracted events."
- **Health check**: iOS updates occasionally invalidate pairing. The nightly
  job's failure (or a backup older than 48h) must surface in the morning
  digest — a silently stale iMessage feed defeats the whole design.

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

## Secrets (`op://homelab/carson`)

Gmail app password; iCloud app-specific password (CalDAV); iPhone backup
encryption password; pairing record (or a separate secret); ICS feed token;
sms-relay API key (and the webhook signing secret for inbound). Ollama
basic-auth is unnecessary in-cluster (ClusterIP bypasses the Traefik gate —
the auth exists for the public host).

## Order of work

Each phase is independently useful; stop anywhere and it still earns its keep.

1. **Core + capture + dates**: chart, SQLite, REST API, web UI behind
   Authelia; contacts + important dates entered by hand; feed.ics published
   and subscribed on both calendars; SMS reminders through sms-relay; manual
   todo capture via the Signal gateway (Claude calling the API). *Already a
   birthday tracker + todo list.*
2. **Calendar ingest**: iCloud CalDAV + Google secret ICS pollers;
   cross-reference; digest gains "today across both calendars."
3. **Email**: IMAP worker, metadata tier (last-contacted + nudges), then
   Ollama transactional extraction → SMS proposal round-trip (send +
   inbound-webhook replies).
4. **iMessage**: pairing, nightly backup CronJob, exporter + delta pipeline
   into the same extraction queue.
5. **Claude tier**: gateway schedule entries — nightly escalation drain,
   morning digest, gift-research ladder.
6. **Enrichment tier**: whitelisted full-content extraction (commitment/todo
   mining from sent mail + iMessage).

## Open questions

- **Where does the phone sleep?** The nightly backup needs the phone on home
  Wi-Fi and ideally on power. If it reliably charges overnight at home this
  is free; if not, the job needs retry windows.
- **hostNetwork CronJob vs. running the backup on zachd-Ubuntu** (like the
  trading watchdog in `scripts/`) with the PVC swapped for an NFS mount.
  Cluster CronJob is more consistent with everything else; the desktop
  already has USB access for the initial pairing. Decide when building
  phase 4. The network-segmentation plan matters here too: post-VLAN, the
  phone (untrusted) and the cluster may no longer share a broadcast domain —
  mDNS discovery breaks across VLANs without a reflector, so the backup
  runner should live wherever the phone's VLAN can reach.
- **Contact seeding**: hand-enter the ~30 people who matter, or import from
  CardDAV/Google Contacts once at setup? (Lean: hand-enter; the list *is*
  the curation.)
- **Group name**: `web/` doesn't fit, `finance/` obviously not. Probably a
  new top-level `life/` group (future home for health tracking etc.).
