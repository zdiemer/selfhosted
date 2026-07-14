#!/usr/bin/env bash
# Apply the current chart + values.local.yaml to the running gamedex release.
#
# Flow:
#   1. helm upgrade --install
#   2. Wait for rollout
#   3. Print pod status
#
# NOTE: rebuild + side-load the image first with ./build.sh if you changed the
# app code or Dockerfile (imagePullPolicy is IfNotPresent — k3s won't re-pull a
# locally-imported tag).

set -euo pipefail

RELEASE="${RELEASE:-gamedex}"
NAMESPACE="${NAMESPACE:-games}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"
LOCAL_VALUES="${HERE}/values.local.yaml"
VALUE_ARGS=(-f "$VALUES")
[[ -f "$LOCAL_VALUES" ]] && VALUE_ARGS+=(-f "$LOCAL_VALUES")

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" "${VALUE_ARGS[@]}"

echo "==> Waiting for ${RELEASE} rollout"
$K rollout status "deployment/${RELEASE}" --timeout=180s

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="${RELEASE}"

# Refresh which games are on the NAS. This runs HERE, not in the cluster: the ROM library is a CIFS
# share mounted on the workstation and the k3s nodes can't see it (and shouldn't have to). The index
# is read from romnas's download receipts, not by walking 80TiB — see tools/nas_index.py. Warm, it's
# a couple of seconds; if the share isn't mounted it says so and exits 0, because a deploy from
# another machine is not a failed deploy.
NAS_TOKEN="$(python3 - "$LOCAL_VALUES" <<'PY' 2>/dev/null || true
import re, sys, pathlib
t = pathlib.Path(sys.argv[1]).read_text() if pathlib.Path(sys.argv[1]).is_file() else ""
m = re.search(r'^nas:\s*$.*?^\s+token:\s*"?([^"\n]+)"?', t, re.M | re.S)
print(m.group(1) if m else "")
PY
)"
if [[ -n "$NAS_TOKEN" ]]; then
  echo "==> Refreshing the NAS index"
  NAS_TOKEN="$NAS_TOKEN" python3 "${HERE}/tools/nas_index.py" || echo "    (nas index failed — the app keeps the last one)"
else
  echo "==> Skipping the NAS index (no nas.token in values.local.yaml)"
fi
