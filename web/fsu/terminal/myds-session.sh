#!/bin/bash
# One MyDS REPL session. Much tamer than its neighbour: this is a single C++
# process that reads stdin and never forks, so the bounds here are about
# politeness rather than containment.
#
# The REPL exits cleanly on EOF (it tests the stream, not the string, after
# std::getline), so unlike cloysta-session.sh there is no restart loop and no
# reason to fear the browser closing.

set -eu

# `load common` inserts ~5,100 strings into a hash table that starts with 60,000
# std::list buckets and copies every key by value on the way in. That is fast,
# but it is the one command here that can be made to spin — 60 CPU-seconds is
# several hundred times what a real session needs.
ulimit -t "${FSU_SESSION_CPU_SECONDS:-60}"
ulimit -u 16
ulimit -v 262144
ulimit -c 0

cd "${HOME}"

exec /usr/bin/timeout --foreground -s KILL "${FSU_SESSION_SECONDS:-900}" /usr/libexec/fsu/myds-repl
