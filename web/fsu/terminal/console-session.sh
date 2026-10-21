#!/bin/bash
# One /console session: the 2013 C++ console programs (COP 3014) and the 2014
# MIPS programs (CDA 3100), behind a menu. ttyd forks one of these per browser
# connection.
#
# WHICH PROGRAM. ttyd runs with --url-arg, so /console/?arg=craps arrives here as
# $1. That string comes from the visitor's URL and is therefore only ever
# compared against the fixed list below — never executed, never interpolated
# into a path. Anything unrecognised gets the menu.
#
# None of these programs forks or execs anything, so this is a far tamer
# neighbour than cloysta-session.sh. What they DO share with Cloysta is the
# failure that makes a terminal demo dangerous to the browser rather than to the
# pod: an infinite output loop. Measured, before any of the bounds below:
#
#   * Both Delta Tau programs (p3 and the earlier draft) read with `cin >> int`
#     inside `while (cont == 'Y')`. Type a letter and cin enters its fail state,
#     every later read fails without blocking, `cont` keeps its 'Y', and the
#     loop prints its prompts forever — 3 MB in under two seconds.
#   * The drink machine does the same, on a letter or on EOF. (Craps and the
#     walking calculator do not: one reads chars, the other reads only once.)
#   * Under spim, read_int at EOF returns 0 indefinitely, so the stats program
#     never sees its negative terminator, writes off the end of its 100-word
#     array, and then raises "Bad address" exceptions without end.
#
# So every program runs with stdout through `head -c`, which ends the flood at
# 1 MiB by SIGPIPE whatever the cause; `stty eof undef` so a stray Ctrl-D cannot
# start one; and a small per-run CPU limit as the backstop.

set -u

# The wall-clock bound, around the whole session rather than each program: a
# visitor idling at the menu holds a --max-clients slot just as surely as one
# idling inside craps. Re-exec under timeout once, keeping $1.
#
# --foreground for the reason cloysta-session.sh spells out (without it the
# session leaves the terminal's foreground process group, and the first read()
# stops it with SIGTTIN). The cost is that timeout kills only this script; the
# program it was running is in the same process group, and gets the SIGHUP when
# the session leader exits and the PTY goes away.
if [ -z "${FSU_CONSOLE_TIMED:-}" ]; then
    FSU_CONSOLE_TIMED=1 exec /usr/bin/timeout --foreground -s KILL \
        "${FSU_SESSION_SECONDS:-900}" "$0" "$@"
fi

ulimit -u 64
ulimit -v 262144
ulimit -c 0
# Nothing here writes a file. The drink machine and the grader only read theirs.
ulimit -f 64

/usr/bin/stty eof undef 2>/dev/null || true

# A handler, not `trap '' INT`. An IGNORED signal is inherited across exec, so
# '' would make Ctrl-C unable to stop the program it is aimed at. A handler is
# reset to the default in every child, so Ctrl-C ends the program and lands back
# here, where it is a no-op and the menu carries on.
trap 'true' INT

C=/usr/libexec/fsu/cop3014
M=/opt/fsu-console/cda3100
DATA=/opt/fsu-console/cop3014

# key | title | course line
PROGRAMS='walk|Walking Distance Calculator (project 1)|COP 3014, Fall 2013
deltatau|Delta Tau fuel simulation (project 3, as submitted)|COP 3014, Fall 2013
deltatau-draft|Delta Tau fuel simulation (earlier draft)|COP 3014, Fall 2013
craps|Two-player craps (project 4)|COP 3014, Fall 2013
drinks|Drink machine (project 6)|COP 3014, Fall 2013
grader|True/false test grader|COP 3014, Fall 2013
stats|Sum, min, max, mean and variance (MIPS)|CDA 3100, Summer 2014
tictactoe|Tic-tac-toe, as far as it got (MIPS)|CDA 3100, Summer 2014'

run_program() {
    local cmd=()
    case "$1" in
        walk)           cmd=("$C/walk") ;;
        deltatau)       cmd=("$C/deltatau") ;;
        deltatau-draft) cmd=("$C/deltatau-draft") ;;
        craps)          cmd=("$C/craps") ;;
        # machine.txt is opened by relative path, so the working directory is
        # the fixture's directory. It is a stand-in; see projects/cop3014.html.
        drinks)         cmd=("$C/drinks") ;;
        # Opens the absolute path /Users/Zachary/xfile.txt — a 2013 MacBook's
        # home directory, compiled into the binary. The image puts a stand-in
        # file at exactly that path rather than touching the source.
        grader)         cmd=("$C/grader") ;;
        # -quiet drops spim's warnings. It does NOT drop spim's five-line
        # banner ("SPIM Version 8.0 … Loaded: exceptions.s"), and that is left
        # in: it is what running a program under spim looked like in 2014 too.
        stats)          cmd=(/usr/bin/spim -quiet -file "$M/Assignment4_Diemer.s") ;;
        tictactoe)      cmd=(/usr/bin/spim -quiet -file "$M/test.s") ;;
        *) return 1 ;;
    esac

    printf '\n'
    # stdbuf -o0 because stdout is now a pipe, which would make stdio fully
    # buffered: spim's prompts, which do not end in a newline, would never
    # appear before it blocks on read. (iostreams already flush on cin through
    # the tie, but under sync_with_stdio they write through the same buffer.)
    #
    # No exit status is reported: a SIGPIPE from head, the CPU limit and a
    # crash all read the same to a visitor — it stopped.
    ( cd "$DATA" && ulimit -t 10 && exec /usr/bin/stdbuf -o0 "${cmd[@]}" ) 2>&1 \
        | /usr/bin/head -c 1048576 || true
    printf '\n\n[program ended]\n'
}

menu() {
    printf '\n  2013–14 console programs, Florida State\n\n'
    local i=0 key title course
    while IFS='|' read -r key title course; do
        i=$((i + 1))
        printf '  %d) %-52s %s\n' "$i" "$title" "$course"
    done <<< "$PROGRAMS"
    printf '\n  q) quit\n\n  Choose: '
}

# Bash builtins only, in these two and in menu(). The pod's PATH is
# /usr/local/bin — the visitor's curated set — which has no grep, cut or sed, and
# an external tool missing here fails SILENTLY: the lookup comes back empty and
# every choice falls through to the menu. That shipped to a test run once.
key_for() {
    local i=0 key rest
    while IFS='|' read -r key rest; do
        i=$((i + 1))
        if [ "$i" = "$1" ]; then printf '%s' "$key"; return 0; fi
    done <<< "$PROGRAMS"
    return 1
}

# Valid only if it is exactly one of the keys: a whole-string comparison, so
# nothing in the URL is ever treated as a pattern.
is_program() {
    local key rest
    while IFS='|' read -r key rest; do
        if [ "$key" = "$1" ]; then return 0; fi
    done <<< "$PROGRAMS"
    return 1
}

printf '  This session ends after %s wall-clock seconds.\n' "${FSU_SESSION_SECONDS:-900}"
printf '  Ctrl-C stops the running program and returns here.\n'

current="${1:-}"
is_program "$current" || current=""

while :; do
    if [ -n "$current" ]; then
        run_program "$current"
        printf '[Enter] run it again   [m] menu   [q] quit: '
        IFS= read -r ans || exit 0
        case "$ans" in
            q|Q) exit 0 ;;
            m|M) current="" ;;
        esac
        continue
    fi

    menu
    IFS= read -r ans || exit 0
    case "$ans" in
        q|Q) exit 0 ;;
        [1-9])
            k="$(key_for "$ans")"
            if [ -n "$k" ]; then current="$k"; fi
            ;;
    esac
done
