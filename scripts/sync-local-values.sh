#!/usr/bin/env bash
# DEPRECATED — forwards to scripts/secrets.sh publish.
#
# This used to tar every values.local.yaml straight into the claude-workspace
# pod. That was push-only: the pod could not refresh itself, so every restart
# (and the pod restarts often — tmux dies with it) needed someone at this
# laptop to re-run the copy. It also found files by `-name values.local.yaml`,
# which silently missed web/apartment-watch/criteria.yaml.
#
# `secrets.sh publish` writes the same set — plus criteria.yaml — into a Secret
# in the claude namespace, then applies it in the pod if it's up. The pod pulls
# for itself with `secrets.sh pull --from-cluster`, so a restart no longer needs
# this machine at all.
#
# Storing the bundle in a Secret grants the pod nothing: it already runs as a
# cluster-admin SA (dev/claude-workspace/templates/rbac.yaml) and can read every
# Secret in the cluster, including the rendered chart Secrets holding these same
# values.
#
# This shim exists so muscle memory and old docs keep working. Use secrets.sh.

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "note: sync-local-values.sh is deprecated — forwarding to 'secrets.sh publish'." >&2
echo "      Pod-side refresh is now 'scripts/secrets.sh pull --from-cluster'." >&2
echo >&2

exec "${HERE}/secrets.sh" publish "$@"
