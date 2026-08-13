#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running claude-workspace
# release.
#
# Flow:
#   1. helm upgrade --install
#   2. Wait for the rollout
#   3. Print pod status

set -euo pipefail

RELEASE="${RELEASE:-claude-workspace}"
NAMESPACE="${NAMESPACE:-claude}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
# The secrets are resolved from 1Password into memory for the life of this run
# and never written to a disk. Each helm call spells out its own `-f <(sv_fd)`
# rather than carrying it in VALUE_ARGS: that fd is a pipe, readable once, so a
# shared one would hand the second reader an empty values file. See
# scripts/lib/secret-values.sh.
. "${HERE}/../../scripts/lib/secret-values.sh"
sv_load "$HERE" || exit 1

VALUE_ARGS=(-f "$VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# Refuse to roll onto a tag that isn't in the registry (ported from
# infra/sms-relay). strategy: Recreate tears the pod down *before* the
# replacement pulls, so a missing tag doesn't fail safe — it takes every
# surface (term, Happy, bakery, messaging) down until someone notices.
REPO="$(awk '/^  repository:/{gsub(/["'"'"']/,"",$2); print $2; exit}' "$VALUES")"
TAG="$(awk '/^  tag:/{gsub(/["'"'"']/,"",$2); print $2; exit}' "$VALUES")"
if [[ "$REPO" == ghcr.io/* && -n "$TAG" ]] && command -v curl >/dev/null && command -v python3 >/dev/null; then
  echo "==> Verifying ${REPO}:${TAG} exists"
  IMG_PATH="${REPO#ghcr.io/}"
  # Package is public, so an anonymous pull token suffices; reuse the GHCR PAT
  # from build.sh when present (harmless, and covers a future private flip).
  BASIC="$(python3 - "$HOME/.docker/config.json" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as fh:
        print(json.load(fh).get("auths", {}).get("ghcr.io", {}).get("auth", ""))
except Exception:
    print("")
PY
)"
  TOKEN="$(curl -fsSL ${BASIC:+-H "Authorization: Basic ${BASIC}"} \
           "https://ghcr.io/token?scope=repository:${IMG_PATH}:pull&service=ghcr.io" \
           | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))' 2>/dev/null || true)"
  if [[ -z "$TOKEN" ]]; then
    echo "    skipped (no GHCR token — cannot verify)" >&2
  else
    # Accept BOTH single manifests and manifest lists/indexes. A registry
    # answers 404 — not 406 — when the tag exists but no Accept type matches,
    # so a missing index type reads as "image was never pushed" and blocks a
    # perfectly good deploy. Docker produces an OCI *index* by default now
    # (v7 was the first tag built that way), which is exactly how this was found.
    CODE="$(curl -sL -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${TOKEN}" \
            -H 'Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json' \
            "https://ghcr.io/v2/${IMG_PATH}/manifests/${TAG}" || echo 000)"
    if [[ "$CODE" != "200" ]]; then
      echo "ERROR: ${REPO}:${TAG} is not in the registry (HTTP ${CODE})." >&2
      echo "       Run build.sh first — deploying now would take the workspace down." >&2
      exit 1
    fi
    echo "    ok"
  fi
fi

# Recover from a half-written release record before trying to add to it.
#
# This chart can upgrade ITSELF from the workspace pod, and `strategy: Recreate`
# deletes that pod the moment the new template is applied — mid-`helm upgrade`.
# Helm writes the new revision as `pending-upgrade` first and only marks it
# `deployed` once the apply returns, so being killed in between leaves a
# permanently pending revision, and every later upgrade fails with "another
# operation (install/upgrade/rollback) is in progress". The SIGTERM guard below
# is what stops this happening; this is the cleanup for records already stuck.
#
# The repair is "forget the dead revision", not "assume it worked": a killed run
# may or may not have applied its manifests, so the pending revision is dropped,
# the previous one is restored to `deployed`, and the ordinary upgrade below
# re-applies everything and converges either way.
STATUS="$(helm status "$RELEASE" -n "$NAMESPACE" -o json 2>/dev/null \
          | python3 -c 'import sys,json; print(json.load(sys.stdin)["info"]["status"])' 2>/dev/null || echo none)"
if [[ "$STATUS" == pending-* ]]; then
  echo "==> Release is ${STATUS} from an interrupted run; repairing"
  python3 - "$RELEASE" "$NAMESPACE" <<'PY'
import base64, gzip, json, subprocess, sys

release, namespace = sys.argv[1], sys.argv[2]

def kubectl(*args, capture=True):
    return subprocess.run(["kubectl", "-n", namespace, *args],
                          capture_output=capture, text=True, check=True).stdout

secrets = json.loads(kubectl(
    "get", "secrets", "-l", f"owner=helm,name={release}", "-o", "json"))["items"]
secrets.sort(key=lambda s: int(s["metadata"]["labels"]["version"]))
if not secrets:
    sys.exit(0)

stuck = secrets[-1]
print(f"    dropping revision {stuck['metadata']['labels']['version']} "
      f"({stuck['metadata']['labels']['status']})")
kubectl("delete", "secret", stuck["metadata"]["name"], capture=False)

if len(secrets) < 2:
    sys.exit(0)

# Helm refuses to upgrade a release whose newest revision isn't `deployed`, so
# the one we fell back to has to be relabelled — in the gzipped payload as well
# as the secret label, since helm reads the payload.
prev = secrets[-2]
version = prev["metadata"]["labels"]["version"]
payload = json.loads(gzip.decompress(base64.b64decode(base64.b64decode(prev["data"]["release"]))))
payload["info"]["status"] = "deployed"
patch = json.dumps({
    "metadata": {"labels": {"status": "deployed"}},
    "data": {"release": base64.b64encode(
        base64.b64encode(gzip.compress(json.dumps(payload).encode()))).decode()},
})
kubectl("patch", "secret", prev["metadata"]["name"], "--type", "merge", "-p", patch,
        capture=False)
print(f"    revision {version} restored as the current one")
PY
fi

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
# Ignore SIGTERM for the duration of the upgrade. When this chart upgrades
# itself, applying the new template deletes this very pod, and the kubelet's
# SIGTERM would otherwise kill the shell — and helm with it — in the seconds
# between "pending-upgrade written" and "deployed written". An ignored signal
# disposition survives exec, so helm inherits this and runs to completion inside
# the pod's termination grace period, which is orders of magnitude longer than
# it needs. The pod still goes down; it just doesn't take the release record
# with it.
trap '' TERM
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}" -f <(sv_fd) --cleanup-on-fail
trap - TERM

echo "==> Waiting for ${RELEASE} rollout"
# Expected to be cut short when upgrading from inside the pod being replaced:
# by here the release is safely recorded and the cluster converges on its own,
# so losing the watch is cosmetic. Don't fail the script over it.
$K rollout status "deployment/${RELEASE}" --timeout=300s \
  || echo "    (rollout watch ended early — normal when self-deploying)"

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"
