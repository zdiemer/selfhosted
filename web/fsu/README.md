# fsu

`fsu.zachd.duckdns.org` — Florida State coursework from 2013–16, running rather
than screenshotted. Tailnet only.

| | | | |
|---|---|---|---|
| **Cloysta** | a Unix shell in C | Operating Systems, Fall 2015, with Taylor Ereio | `/cloysta` |
| **Breakout** | Java Swing game | Fall 2014, with Adam Greenstein | in-browser JVM |
| **pybank** | Django 1.9 "online bank" | Spring 2016, with Wenqi Wang | `/pybank` |
| **my-data-structure** | C++ chained hash table | January 2015 — course *inferred*, nothing in the repo says | `/hashtable` |
| **cop3014** | six C++ console programs | COP 3014 Programming I, Fall 2013 | `/console` |
| **cda3100** | MIPS (spim) + Verilog | CDA 3100 Computer Organization I, Summer 2014 | `/console` + build-time sims |
| **python-homework4** | two Python 2 scrapers | Python Programming, Spring 2016 | build-time run |
| **cen4021-notepadpp** | a 13-line Notepad++ change | CEN 4021 Software Engineering II, Spring 2015 | page + diff |
| **cop4656-android** | an Android toggle app | Summer 2015 | page + APK |

The last five are **private** repos, recovered from a Dropbox folder in 2026
rather than from GitHub; their source links 404 for anyone but their owner.
One file that came with them, `tictoetest.s`, was left out: it has no name on
it and its style matches nothing else here, so its authorship is unknown.

## The rule this chart is built around

**The project repositories are never modified.** They are git submodules under
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
Dockerfile.web       nginx + the static site + the Breakout jar + build-time
                     transcripts (Verilog sims, grader, Notepad++ diff, Python)
Dockerfile.terminal  Cloysta, the MyDS REPL and the /console menu, three ttyds
Dockerfile.pybank    Django 1.9.5 on Python 2.7 (four stages, see below)
site/                hand-written HTML/CSS/JS, no build step, no npm
terminal/            *-session.sh, repl.cpp, words/, cop3014/ (stand-in data)
sims/                watch-only Verilog harnesses for the CDA 3100 testbenches
pyhw4/               requests stand-in, Wayback manifest, invented imgur data
pybank/              settings_shim.py, urls_shim.py, seed.py, entrypoint.sh
projects/            the nine submodules, read-only
```

Three images from one build context. `build.sh` hard-fails unless all three
`images.*.tag` values equal `Chart.yaml` `appVersion` — one version number for
the whole showcase, so "which of the three did I forget to bump" is never a
question. Note that `buildctl` has no `-f`: `--local dockerfile=` takes a
*directory* and the filename is `--opt filename=`.

## Routing, and the trap in it

One hostname, five path prefixes, two Ingress objects.

| Path | Backend | Auth |
|---|---|---|
| `/` | `fsu-web` | none |
| `/pybank` | `fsu-pybank` | none |
| `/cloysta` | `fsu-terminal:7681` | Authelia forwardAuth |
| `/hashtable` | `fsu-terminal:7682` | Authelia forwardAuth |
| `/console` | `fsu-terminal:7683` | Authelia forwardAuth |

Two objects is **forced**, not stylistic: `router.middlewares` is an annotation
on the Ingress and Traefik applies it to every router that Ingress produces, so
there is no per-path middleware. The second Ingress adds authentication to
the three terminal paths.

> **Traefik v3's `PathPrefix` is a raw string prefix**, unlike Kubernetes
> `pathType: Prefix`, which is element-wise. So the `/cloysta` rule also matches
> `/cloysta.html`, and `/pybank` also matches `/pybank.html`. This shipped once:
> the explainer pages were at the document root, and `/cloysta.html` bounced off
> Authelia while `/pybank.html` was handed to Django, which 404'd it — both with
> a perfectly healthy nginx behind them. Hence `site/projects/`, and hence the
> reserved-prefix assert in `Dockerfile.web`, which now refuses to build a page
> whose served path starts with `cloysta`, `hashtable`, `pybank` or `console`.

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

## /console: eight programs, one terminal

The six COP 3014 programs and the two CDA 3100 MIPS programs share one ttyd,
behind `terminal/console-session.sh`. ttyd runs with `--url-arg`, so each page
opens it on its own program (`/console/?arg=craps`); the argument is compared
against a fixed list and never executed, and `Dockerfile.terminal` asserts that
a hostile one lands on the menu.

None of them forks, so the danger is to the *browser*: three of them loop
forever printing prompts on a letter or EOF (both Delta Taus, the drink
machine), and spim's `read_int` returns 0 at EOF forever. Every program runs
through `head -c 1048576` with `stdbuf -o0` (otherwise spim's prompts never
appear through the pipe), and the session disables Ctrl-D, Cloysta-style.

Two programs read data files nobody saved, and get **stand-ins written in
2026**: `terminal/cop3014/machine.txt`, and the grader's
`/Users/Zachary/xfile.txt`, generated by `make-xfile.py` with bare-CR line
endings because that is what the parser splits on. The pages say so.

## Run at build time instead

Programs whose output does not depend on a visitor run once in `Dockerfile.web`
and are served as text under `/projects/out/`, which `site/capture.js` loads
into the pages. The build fails if any of them stops saying what the page says
it does.

- **Verilog.** The testbenches have no `$display` or `$finish` — they were for
  ModelSim's waveform window. `sims/harness_*.v` is a second root module that
  reads them by hierarchical reference (`iverilog -s test_bench -s harness`),
  so the 2014 files compile unchanged. hw8 must reproduce the Gray sequence in
  the 2014 screenshot.
- **The grader, twice.** Its histogram bars depend on operand evaluation
  order, which C++ fixed only in C++17; it is built normally and with
  `-fstrong-eval-order=none`, and the two outputs must differ.
- **Notepad++.** Diffed against `FindReplaceDlg.cpp` at 6.7.4, pinned by commit
  and sha256; exactly 13 added lines.
- **Python homework.** `pyhw4/shim/requests.py` replaces `requests` and answers
  only from recordings. The faculty scraper reads **Wayback Machine captures**
  pinned in `pyhw4/wayback.tsv` — fetched at build time, never committed,
  because they are 2016 staff contact pages and this repo is public. The fetch
  is slow on purpose (archive.org refuses connections after ~18 quick
  requests), and BuildKit caches it. The imgur scraper reads **invented** data
  (`pyhw4/imgur/README`) built to trigger its empty-winners bug.

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
