# fsu

`fsu.zachd.duckdns.org` — four pieces of Florida State coursework from 2014–16,
running rather than screenshotted. Tailnet only.

| | | |
|---|---|---|
| **Cloysta** | a Unix shell in C | Operating Systems, Fall 2015, with Taylor Ereio |
| **Breakout** | Java Swing game | Fall 2014, with Adam Greenstein |
| **pybank** | Django 1.9 "online bank" | Spring 2016, with Wenqi Wang |
| **my-data-structure** | C++ chained hash table | January 2015 — course *inferred*, nothing in the repo says |

## The rule this chart is built around

**The four repositories are never modified.** They are git submodules under
`projects/`, which `scripts/submodules-lock.sh` makes read-only on disk and
`.claude/settings.json` denies writes to. Every adjustment an eleven-year-old
program needs in 2026 — a compiler flag, a settings shim, a REPL harness, a
session wrapper — lives outside them, in this directory.

That inverts the repo's usual submodule direction, the same way
[`web/old-diemer-codes/`](../old-diemer-codes/) does and for the same reason:
`docker build` cannot `COPY` from outside its context, so the sources sit
*inside* the chart directory and the chart owns them rather than the other way
round. A useful side effect is that the build only ever reads them, so no
submodule worktree can be dirtied by a build.

## Layout

```
Dockerfile.web       nginx + the static site + the Breakout jar (JDK builder stage)
Dockerfile.terminal  Cloysta + the MyDS REPL, each behind its own ttyd
Dockerfile.pybank    Django 1.9.5 on Python 2.7 (four stages, see below)
site/                hand-written HTML/CSS, no build step, no npm
terminal/            cloysta-session.sh, myds-session.sh, repl.cpp, words/
pybank/              settings_shim.py, urls_shim.py, seed.py, entrypoint.sh
projects/            the four submodules, read-only
```

Three images from one build context. `build.sh` hard-fails unless all three
`images.*.tag` values equal `Chart.yaml` `appVersion` — one version number for
the whole showcase, so "which of the three did I forget to bump" is never a
question. Note that `buildctl` has no `-f`: `--local dockerfile=` takes a
*directory* and the filename is `--opt filename=`.

## Routing, and the trap in it

One hostname, four path prefixes, two Ingress objects.

| Path | Backend | Auth |
|---|---|---|
| `/` | `fsu-web` | none |
| `/pybank` | `fsu-pybank` | none |
| `/cloysta` | `fsu-terminal:7681` | Authelia forwardAuth |
| `/hashtable` | `fsu-terminal:7682` | Authelia forwardAuth |

Two objects is **forced**, not stylistic: `router.middlewares` is an annotation
on the Ingress and Traefik applies it to every router that Ingress produces, so
there is no per-path middleware. Same shape as `life/carson`, inverted — carson's
second Ingress removes auth from one path, this one adds it to two.

> **Traefik v3's `PathPrefix` is a raw string prefix**, unlike Kubernetes
> `pathType: Prefix`, which is element-wise. So the `/cloysta` rule also matches
> `/cloysta.html`, and `/pybank` also matches `/pybank.html`. This shipped once:
> the explainer pages were at the document root, and `/cloysta.html` bounced off
> Authelia while `/pybank.html` was handed to Django, which 404'd it — both with
> a perfectly healthy nginx behind them. Hence `site/projects/`, and hence the
> reserved-prefix assert in `Dockerfile.web`, which now refuses to build a page
> whose served path starts with `cloysta`, `hashtable` or `pybank`.

Tailnet-only is `ingress.cloudflareHosts: []` plus `infra/duckdns` running
`mode: tailnet`. There is deliberately no `ipAllowList` — see
`infra/hatch/values.yaml` for the measurement showing it would 403 everything.

## Cloysta: what the container is for

This program resolves `argv[0]` against `PATH` and `execv`s it. The container is
the security boundary, not the shell — so it has no credentials, no
service-account token, a read-only root filesystem, and a NetworkPolicy with
`egress: []`. **Inside the shell, `ping`, `curl` and anything DNS hang and then
time out. That is the policy working**, not a bug.

Kubernetes has no per-process limits, so those live in `terminal/cloysta-session.sh`:
`ulimit -u` bounds a fork bomb, `ulimit -t` bounds CPU seconds, and a wall-clock
`timeout` bounds the session. Three things in that script are load-bearing and
each cost a debugging session to find:

- **`timeout --foreground`.** Without it, GNU `timeout` calls `setpgid(0,0)`, which
  leaves Cloysta outside the terminal's foreground process group. Its first
  `read()` then raises `SIGTTIN` and the shell stops: keys still echo, because the
  line discipline does that, but nothing is ever executed and the session looks
  silently dead.
- **No `setsid`.** For the same family of reasons — it would suppress the SIGHUP
  that ttyd relies on to clean up a disconnected session.
- **`stty eof undef`.** `REPL/read.c` throws away the `fgets` return value and
  always returns 1, so the shell cannot detect EOF and the `run = 0` branch in
  `main.c` is unreachable. A bare Ctrl-D therefore spins forever reprinting the
  prompt — measured at ~42 MB in 13 seconds, all of it down the websocket.
  Removing `VEOF` from the line discipline means Ctrl-D delivers a literal `0x04`
  instead, and the only way out becomes `exit`, which is the only exit Cloysta
  itself implements. `ulimit -t` remains the backstop.

`/bin/ls` and `/bin/more` must exist at exactly those paths: `mpipe()` ignores
what you typed and always runs that hardcoded pair. The Dockerfile asserts it.

`/usr/local/bin` is the visitor's entire command set — eighteen boring
utilities — while ttyd, the two programs and the session wrappers live in
`/usr/libexec/fsu` and are invoked by absolute path. So "what can be run from
inside the shell" is answered by listing one directory, and the Dockerfile
asserts nothing leaked into it.

Measured on this cluster: the kubelet's `podPidsLimit` is `-1`, so **`ulimit -u`
is the only pid bound in the stack**, not defence in depth. Worth rechecking if
the shell ever seems to survive a fork bomb.

## pybank: the path prefix

`settings_shim.py` carries the full argument; the short version is that the
obvious approach — Traefik `StripPrefix` plus `FORCE_SCRIPT_NAME` — **does not
work, and fails silently**. `FORCE_SCRIPT_NAME` only affects `reverse()`, and
`StaticFilesHandler.__call__` gates on the raw `PATH_INFO`, so no value of
`STATIC_URL` serves both the browser and Django. The page still returns 200 and
every stylesheet 404s.

So there is no `StripPrefix` anywhere and no `FORCE_SCRIPT_NAME`. The prefix
lives in an outer URLconf (`urls_shim.py`), which is the thing `reverse()`
actually walks, and browser, Traefik, Django and `StaticFilesHandler` all agree
on the path. `Dockerfile.pybank` asserts all of it at build time.

Python 2.7 is not nostalgia: `main/views.py` uses an implicit relative import and
mixes tabs with spaces, either of which stops Python 3 at parse time. The image
is four stages so that every network fetch and every source rewrite happens on a
modern base — `python:2.7-slim-buster` has a 2020 pip, a 2020 CA bundle, and apt
sources that moved to `archive.debian.org` in 2024.

**Generating `en_US.UTF-8` is load-bearing.** `views.py` calls
`locale.setlocale(LC_ALL, '')` outside a `try` and `locale.currency()` inside
one, so without a real locale every balance on the site silently renders as `$0`
while the page looks perfectly fine.

## Breakout: CheerpJ

The 2014 bytecode, compiled `--release 8` and run client-side on CheerpJ 4.3's
WebAssembly JVM. Nothing is rewritten and no JVM runs on the server.

`images/` must be a **top-level jar entry**, a sibling of `breakout/` — `Ball.java`
does `getResource("/images/ball.png")` with a leading slash. Get it wrong and
`ImageIO.read` returns null, the constructor's catch prints to a stdout nobody
reads, and the next line throws: the game dies before the window appears. The
Dockerfile asserts the entry's position and the class file's major version.

Verified working in a real browser: main menu, new game, gameplay through to
game over, Esc pause, and **Save**, which puts up its "Game Saved" dialog — the
CWD under CheerpJ is the writable, IndexedDB-backed `/files/`, so the save lands
in the visitor's browser. No COOP/COEP headers are needed; `nginx/default.conf`
says what to do if a future version ever wants them. CheerpJ is used here under
its Community License, which covers personal projects and unlimited use of the
`cjrtnc.leaningtech.com` CDN, and asks for credit — which is in the page footer.

## Deploying

```sh
./build.sh     # three images -> registry.zachd.duckdns.org/zdiemer/fsu-*
./upgrade.sh   # namespace, helm upgrade --install, rollouts, then the checks
```

No `sv_load`: there are no secrets in this chart, and the one-line reason is in
`upgrade.sh`. The post-deploy checks WARN rather than fail on the HTTP probes
(a deploy run from off-tailnet legitimately cannot reach the host) but hard-fail
on the declarative ones. The check that matters:

```sh
curl -s -o /dev/null -w '%{http_code}' https://fsu.zachd.duckdns.org/cloysta/
```

A 3xx is correct. **A 200 means the forwardAuth middleware did not take and a
fork/exec shell is open to everything on the tailnet.**
