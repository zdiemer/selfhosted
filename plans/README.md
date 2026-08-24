# plans

Designs for work that isn't done yet, one Markdown file per plan.

Everything else in this repo documents something that *runs* — a chart, its
values, the reasoning behind its current shape. This folder is the other half:
changes that are decided but not deployed, usually because they're blocked on
hardware, on a purchase, or on a maintenance window.

A plan earns a file here when the reasoning behind it is worth more than the
diff that eventually implements it — the constraints that were measured, the
approaches that were ruled out and why, the order the steps have to happen in.
That context is expensive to rebuild and easy to lose between the conversation
that produced it and the evening months later when there's time to do the work.

When a plan is executed, the durable parts move into the READMEs of whatever it
touched and the file is deleted. A plan left here after its work has shipped is
a stale plan, and will be read as a live one.

| Plan | What it covers | Status |
|---|---|---|
| [`network-segmentation.md`](network-segmentation.md) | Splitting the flat `192.168.4.0/22` home LAN into cluster / trusted / untrusted VLANs, with the NAS dual-homed by direct IP on two of them and the SMS relay handset moved to wired isolation. Requires replacing the eero's routing and the unmanaged switch. | Not started — blocked on hardware |
| [`carson.md`](carson.md) | "Mr. Carson" — personal CRM / assistant: todo tracker, birthday/anniversary reminders with Claude-researched gift links, calendar assistant (reads iCloud + Google, publishes its own ICS feed), and an email/iMessage watcher (IMAP + nightly `idevicebackup2` wireless backups) with Ollama doing bulk extraction and gateway-scheduled Claude runs doing the judgment work. | Not started |
