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
LOCAL_VALUES="${HERE}/values.local.yaml"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# Materialize from 1Password when missing, as the other charts do.
if [[ ! -f "$LOCAL_VALUES" && -f "${HERE}/values.local.tpl.yaml" ]] && command -v op >/dev/null 2>&1; then
  echo "==> materializing values.local.yaml from 1Password"
  op inject -i "${HERE}/values.local.tpl.yaml" -o "$LOCAL_VALUES" \
    || { echo "FAIL: op inject failed. Signed in?  eval \$(op signin)"; exit 1; }
  chmod 600 "$LOCAL_VALUES"
fi

# ------------------------------------------------------------------------------
# Pre-flight 1: the repository password
# ------------------------------------------------------------------------------
# restic derives the repository's master key from this. There is no recovery
# path: without it every backup on the NAS is unreadable ciphertext, however
# intact the files are. Losing it is strictly worse than having no backups,
# because you will believe you are covered.
if [[ ! -f "$LOCAL_VALUES" ]]; then
  echo "FAIL: missing ${LOCAL_VALUES}"
  echo "      cp values.local.yaml.example values.local.yaml, generate a password"
  echo "      (openssl rand -base64 32), then:"
  echo "        ./scripts/secrets.sh import infra/k8up/values.local.yaml"
  exit 1
fi
if grep -q 'CHANGE-ME' "$LOCAL_VALUES"; then
  echo "FAIL: restic.password is still the placeholder in ${LOCAL_VALUES}"
  exit 1
fi

# If a repository already exists, the password must still open it. Changing it
# does not re-encrypt anything — it silently starts a SECOND repository at the
# same path and every existing snapshot becomes unreachable.
if ! kubectl get secret -n "$NAMESPACE" k8up-restic-password >/dev/null 2>&1; then
  echo "    (no existing repository password in-cluster — first install)"
else
  LIVE="$(kubectl get secret -n "$NAMESPACE" k8up-restic-password -o jsonpath='{.data.password}' 2>/dev/null | base64 -d)"
  WANT="$(grep -A2 '^restic:' "$LOCAL_VALUES" | sed -n 's/^ *password: *"\{0,1\}\([^"]*\)"\{0,1\}.*/\1/p' | head -1)"
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
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" -f "$VALUES" -f "$LOCAL_VALUES" --cleanup-on-fail

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
