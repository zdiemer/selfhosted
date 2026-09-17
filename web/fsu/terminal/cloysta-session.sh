#!/bin/bash
# One Cloysta session. ttyd forks one of these per browser connection.
#
# Every bound here is a PER-PROCESS bound, which is precisely the set Kubernetes
# cannot express: a pod has no fork limit, no CPU-seconds limit and no per-file
# size limit. The chart handles what it can (cpu limit, memory limit, read-only
# rootfs, egress: [], no service-account token); this handles the rest.
#
# NO setsid, DELIBERATELY. ttyd's forkpty() child is already a session leader
# with the PTY slave as its controlling terminal, and that is the only thing
# that reliably kills Cloysta when the browser goes away. REPL/read.c discards
# the fgets() return value and always returns 1:
#
#     fgets(str, 255, stdin);      /* return value dropped */
#     strcpy(line, str);           /* at EOF, str is uninitialised malloc */
#     return line == NULL ? 0 : 1; /* line is never NULL, so: always 1 */
#
# so the `run = 0` EOF branch in main.c's shell_loop is dead code and at EOF the
# shell spins forever on uninitialised heap. Putting it in its own session would
# suppress the kernel's SIGHUP, dodge ttyd's killpg, and kill `timeout` before
# the shell — leaving an immortal orphan at 100% CPU. See site/cloysta.html.

set -eu

# ulimit -t is the real safety net for that spin-loop, and the reason a
# wall-clock timeout alone is not enough: an idle session blocks in read() and
# burns ~0 CPU no matter how long it sits there, while a spin burns 60s of CPU
# in 60s of wall clock. A timeout cannot tell those apart. This can, and the
# spinner takes SIGKILL while the person reading the prompt is left alone.
ulimit -t "${FSU_SESSION_CPU_SECONDS:-60}"

# Processes per UID — the fork-bomb bound, and not only that. compute_limits()
# (the `limits` builtin) forks a child that never calls exit() and returns up
# through _execute() into `while(run) shell_loop(...)`; a failed background
# execv() does the same, because that branch has no exit(1) after it. Both leave
# a duplicate shell racing for stdin. This caps how many can accumulate.
#
# Per-UID, so it is shared across concurrent sessions: a fork bomb in one denies
# the other. That is the intended trade at --max-clients 2.
ulimit -u 64

# Bounds `echo hi > file`. The only writable paths are in-memory emptyDirs with
# their own sizeLimit, so this is the inner of two bounds.
ulimit -f 2048

# Address space. _prompt() leaks a 128-byte strdup of "local" on every single
# prompt when MACHINE is unset (it is set, but the leak is one of several), and
# _read() mallocs 255*sizeof(char*) — 8x what it needs — per line.
ulimit -v 262144

# This program segfaults as a matter of routine. No 256MB cores in a tmpfs.
ulimit -c 0

cd "${HOME}"

# DISABLE THE EOF CHARACTER ON THIS PTY. This is the single most important line
# in the file, and it is here rather than in the source because the source is a
# frozen archive.
#
# Because _read() never detects EOF (see the header comment), a bare Ctrl-D puts
# Cloysta into a tight loop that reprints the prompt forever. Measured: roughly
# 42 MB of "guest@linprog: /tmp => " in 13 wall-clock seconds, all of it down
# the websocket and into the visitor's tab. ulimit -t does stop it, but only
# after the flood.
#
# `stty eof undef` removes VEOF from the line discipline, so Ctrl-D delivers a
# literal 0x04 into the line buffer instead of signalling end-of-file, and the
# read() that would have returned 0 never happens. The only way out becomes the
# `exit` builtin — which is exactly the only exit Cloysta itself implements.
#
# A browser disconnect is unaffected and still works: ttyd kills the process
# group, which is why this script must not call setsid.
#
# || true because this also has to run when stdin is a pipe (CI, `docker run
# -i`), where there is no line discipline to configure.
/usr/bin/stty eof undef 2>/dev/null || true

cat <<'BANNER'

  Cloysta — a simple shell for Linux
  Florida State University, Operating Systems, Fall 2015
  Zachary Diemer and Taylor Ereio

  Try:  ls  ·  pwd  ·  echo $HOME  ·  cd /usr  ·  echo hi > f  ·  cat f
        limits  ·  etime ls  ·  export FOO=bar  ·  echo $FOO  ·  exit

  Type `exit` to leave. Ctrl-D is deliberately disabled — see below.

  Known, and left exactly as submitted:
    * `export FOO=bar` then `echo $FOO` says "not found". export does
      putenv(args[1]), and args is main()'s stack array, which _setup() zeroes
      at the top of every loop — so the variable is gone before you can read it.
    * `ls | more` ignores what you typed. REPL/redirect.c:mpipe() is hardcoded
      to /bin/ls | /bin/more and prints a warning saying so.
    * `cd` strcat's onto an uninitialised buffer and may crash the shell. It
      will restart.
    * _read() drops the fgets() return value, so the shell cannot detect EOF and
      spins forever printing the prompt. This session disables the terminal's
      EOF character so you cannot fall into it by accident.
    * There is no history and no line editing. Arrow keys emit escape codes.

BANNER

# Not inside the heredoc: it is quoted so that `$HOME` and `$FOO` above stay
# literal, which is the whole point of printing them.
printf '  This session ends after %s wall-clock seconds or %s CPU-seconds.\n\n' \
    "${FSU_SESSION_SECONDS:-900}" "${FSU_SESSION_CPU_SECONDS:-60}"

# Restart loop, capped. chgdir() does `char path[510];` then strcat(path, "PWD=")
# onto that uninitialised buffer and putenv()s a pointer into a stack array, so a
# crash mid-demo is expected rather than exceptional. Restarting keeps the demo
# usable; the cap keeps a crash-loop from becoming a busy-loop; the outer
# `timeout` bounds the whole thing regardless.
i=0
while [ "$i" -lt 20 ]; do
    i=$((i + 1))

    # `--foreground` IS LOAD-BEARING, not a style choice. GNU timeout normally
    # calls setpgid(0,0) so it can signal the whole process tree — which leaves
    # cloysta in a process group that is NOT the terminal's foreground group.
    # The first read() from the controlling terminal then raises SIGTTIN and the
    # shell STOPS: characters still echo, because the line discipline is doing
    # that, but nothing is ever executed and the session looks silently dead.
    # Measured exactly that before adding this flag. The cost is that timeout
    # signals only cloysta itself and not its children; `ulimit -u` above is
    # what bounds those, and the pod bounds the rest.
    #
    # This is the same class of hazard as setsid, which is why the header says
    # not to reach for that either.
    #
    # `rc=0; cmd || rc=$?` rather than `if cmd; then`. With an if/then and no
    # else branch, a false condition makes the whole statement return 0, so a
    # trailing `rc=$?` reads 0 and every crash looks like a clean exit. The ||
    # form also keeps `set -e` from killing the loop on the first segfault.
    rc=0
    /usr/bin/timeout --foreground -s KILL "${FSU_SESSION_SECONDS:-900}" /usr/libexec/fsu/cloysta || rc=$?

    if [ "$rc" -eq 0 ]; then
        # The `exit` builtin set run = 0. This is the only path out of main()
        # that Cloysta itself controls.
        exit 0
    fi

    # 124 = wall-clock timeout, 137 = SIGKILL (ours, or the CPU limit).
    # Either way the session is over, not crashed.
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
        printf '\n[session ended: time limit reached]\n'
        exit 0
    fi
    printf '\n[cloysta exited with status %s — restarting (%s/20)]\n' "$rc" "$i"
done

printf '\n[too many restarts; giving up]\n'
exit 1
