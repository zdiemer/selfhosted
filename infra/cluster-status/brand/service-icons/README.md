# Seeded service icons

Drop a favicon here as `<slug>.<png|svg|ico|gif|jpg|webp>` and the tile for that
service uses it instead of fetching one. The slug is the catalog entry's
`service` (or its `slug:` override) in [`../../values.yaml`](../../values.yaml).

**A seeded slug is never fetched over the network** — the collector copies this
directory into `/data/icons` at startup and skips it thereafter. So this is both
the fallback and the override.

Two reasons to put a file here.

**The collector cannot reach the service.** `claude-workspace`, `rachel-workspace`
and `happy-server` carry NetworkPolicies that refuse connections from anything
but their own ingress. That is correct and is not going to be relaxed so a
status page can have a picture. `keepass-keeweb` does not answer plain HTTP on
its Service port at all, and `keepass-webdav` answers `401` to everything —
also correct.

**The service serves a bad one.** `old.diemer.codes` publishes a 152KB
multi-resolution `.ico` at `/favicon.ico`; every phone that opens the dashboard
would download it. The collector prefers whatever `<link rel="icon">` points at
for exactly this reason, but a file here beats both.

Anything else — a service with a perfectly good favicon on its own ClusterIP —
should **not** be seeded. That file would then be a second copy to keep in sync
with an upstream mark that changes without telling us, which is the failure mode
this directory exists to avoid everywhere else.

Where the marks come from, when we have one: our own source trees, which are
either in this repo or pinned as submodules of it (`web/diemer-codes/public/`,
`games/gamedex/static/`, `finance/money/frontend/public/`, and so on). Copy the
file, don't symlink — Helm's `.Files.Glob` does not follow links out of the
chart directory.

Keep them small. The whole ConfigMap shares the 1MiB object ceiling with
nothing, but every one of these is served to every phone that opens the page.
