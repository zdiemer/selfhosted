#!/usr/bin/env bash
# Apply the backup operator, the shared restic repository, and every schedule.
#
# The failure mode this guards against is not a crash — it is a chart that
# installs perfectly and backs nothing up, or backs things up into a repository
# nobody can read. Both look healthy for months.

set -euo pipefail

RELEASE="${RELEASE:-k8up}"
NAMESPACE="${NAMESPACE:-k8up}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# The restic repository password comes from 1Password into memory and never onto
# a disk. Each reader below gets its own `<(sv_fd)` — that fd is a pipe, good for
# exactly one read. See scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

# ------------------------------------------------------------------------------
# Pre-flight 1: the repository password
# ------------------------------------------------------------------------------
# restic derives the repository's master key from this. There is no recovery
# path: without it every backup on the NAS is unreadable ciphertext, however
# intact the files are. Losing it is strictly worse than having no backups,
# because you will believe you are covered.
if ! sv_has; then
  echo "FAIL: no restic password resolved from 1Password."
  echo "      check with:  ./scripts/secrets.sh check infra/k8up"
  exit 1
fi
if sv_fd | grep -q 'CHANGE-ME'; then
  echo "FAIL: restic.password is still the placeholder in the vault item"
  echo "      op://homelab/infra-k8up/values.local.yaml — generate one with"
  echo "      openssl rand -base64 32 and:  ./scripts/secrets.sh edit infra/k8up"
  exit 1
fi

# If a repository already exists, the password must still open it. Changing it
# does not re-encrypt anything — it silently starts a SECOND repository at the
# same path and every existing snapshot becomes unreachable.
if ! kubectl get secret -n "$NAMESPACE" k8up-restic-password >/dev/null 2>&1; then
  echo "    (no existing repository password in-cluster — first install)"
else
  LIVE="$(kubectl get secret -n "$NAMESPACE" k8up-restic-password -o jsonpath='{.data.password}' 2>/dev/null | base64 -d)"
  WANT="$(sv_fd | grep -A2 '^restic:' | sed -n 's/^ *password: *"\{0,1\}\([^"]*\)"\{0,1\}.*/\1/p' | head -1)"
  if [[ -n "$LIVE" && -n "$WANT" && "$LIVE" != "$WANT" ]]; then
    echo "FAIL: restic.password differs from the one already in the cluster."
    echo "      Applying it would not re-key the repository — it would start a new"
    echo "      one at the same path and orphan every existing snapshot."
    echo "      Restore the original password, or move the old repo aside first."
    exit 1
  fi
fi

# ------------------------------------------------------------------------------
# Pre-flight 2: the repository export
# ------------------------------------------------------------------------------
# Created by hand on the TrueNAS — democratic-csi only exports datasets it
# provisioned. If it is missing, every backup pod fails to mount and the
# schedules quietly do nothing.
NAS="$(awk '/^  nfs:/{f=1} f&&/server:/{print $2; exit}' "$VALUES")"
EXPORT="$(awk '/^  nfs:/{f=1} f&&/path:/{print $2; exit}' "$VALUES")"
echo "==> Pre-flight: NFS export ${NAS}:${EXPORT}"
if command -v showmount >/dev/null 2>&1; then
  if showmount -e "$NAS" 2>/dev/null | grep -q "^${EXPORT} "; then
    echo "    [OK] exported"
  else
    echo "FAIL: ${EXPORT} is not exported by ${NAS}."
    echo "      Create the NFS share on the TrueNAS (Shares > Unix NFS), or via the API."
    exit 1
  fi
else
  echo "    SKIP: showmount not installed"
fi

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

# Chart.lock and charts/*.tgz are gitignored local state; refresh them from
# Chart.yaml so a merged chart-bump PR actually deploys the new version.
echo "==> helm dependency update"
helm dependency update "$HERE" >/dev/null

echo "==> helm upgrade --install ${RELEASE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" -f "$VALUES" -f <(sv_fd) --cleanup-on-fail

echo "==> Waiting for the operator"
kubectl -n "$NAMESPACE" rollout status "deployment/${RELEASE}" --timeout=300s

# ------------------------------------------------------------------------------
# Verify
# ------------------------------------------------------------------------------
echo ""
echo "==> Schedules"
kubectl get schedules -A -o custom-columns='NAMESPACE:.metadata.namespace,NAME:.metadata.name,BACKUP:.spec.backup.schedule' 2>/dev/null

echo ""
echo "==> Repository claims (every one must be Bound, or that namespace backs up nothing)"
UNBOUND=0
while read -r ns; do
  [[ -z "$ns" ]] && continue
  phase="$(kubectl -n "$ns" get pvc k8up-restic-repo -o jsonpath='{.status.phase}' 2>/dev/null || echo MISSING)"
  printf '    %-14s %s\n' "$ns" "$phase"
  [[ "$phase" != "Bound" ]] && UNBOUND=$((UNBOUND+1))
done < <(kubectl get schedules -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\n"}{end}' 2>/dev/null | sort -u)

if [[ "$UNBOUND" != "0" ]]; then
  echo ""
  echo "FAIL: ${UNBOUND} namespace(s) cannot reach the repository."
  exit 1
fi

echo ""
echo "==> PreBackupPods (database dumps)"
kubectl get prebackuppods -A -o custom-columns='NAMESPACE:.metadata.namespace,NAME:.metadata.name' 2>/dev/null

cat <<'EOF'

Installed, but UNPROVEN until a restore has been done. A backup that has never
been restored is a belief, not a backup. Before reclaiming the migration's
rollback PVs, prove one end to end — see README.md "Proving a restore".

Quick check that the repository is actually being written:

  kubectl -n k8up get schedules
  kubectl get backups -A            # after the first scheduled run
EOF
