#!/usr/bin/env bash
# Copy the client-derived content the server needs onto the data PVC.
#
# COMP_hack reads two things straight out of the game client, and ships neither:
#
#   BinaryData/        every item, skill, demon, zone and NPC definition. The
#                      server decrypts these with the COMP_hack key, so they
#                      must come from a client already patched for COMP_hack
#                      (the ReIMAGINE client is; a stock Aeria/JP one is not).
#   Map/Zone/Model/*.qmp
#                      zone geometry, for collision and spawn placement.
#
# They are Atlus/Cave copyright, which is why this is a script against a
# client directory you already have rather than a layer in the image.
#
# Usage: ./stage-client.sh /path/to/ReIMAGINE       (the dir with ImagineClient.exe)
#
# Idempotent: the PVC ends up equal to the source for those two trees. Runs a
# one-shot helper pod with the PVC mounted, so it works even while the server
# is down and does not need the game pod's read-only mount to be writable.

set -euo pipefail

RELEASE="${RELEASE:-smt-imagine}"
NAMESPACE="${NAMESPACE:-games}"
SRC="${1:?usage: $0 /path/to/client/dir}"
[[ -d "$SRC/BinaryData/Shield" ]] || { echo "no BinaryData/Shield under $SRC — wrong directory?"; exit 1; }
[[ -d "$SRC/Map/Zone/Model" ]]    || { echo "no Map/Zone/Model under $SRC — wrong directory?"; exit 1; }

K="kubectl -n ${NAMESPACE}"
PVC="${RELEASE}-data"
HELPER="${RELEASE}-stage-$$"

$K get pvc "$PVC" >/dev/null

# A pod per run rather than exec into the game pod: the data mount there is
# read-only, and staging must work before the first deploy has a running pod.
# The game pod holds the RWO claim on its node, so the helper must land on the
# same node — copy the affinity from wherever the claim is currently attached.
NODE="$($K get pod -l app.kubernetes.io/instance="${RELEASE}" -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null || true)"
cleanup() { $K delete pod "$HELPER" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "==> Starting helper pod ${HELPER}${NODE:+ on $NODE}"
$K apply -f - <<EOF >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: ${HELPER}
  labels: { app.kubernetes.io/name: smt-imagine-stage }
spec:
  restartPolicy: Never
  ${NODE:+nodeName: ${NODE}}
  securityContext: { runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000 }
  containers:
    - name: stage
      image: busybox:1.37
      command: ["sh", "-c", "sleep 3600"]
      volumeMounts: [{ name: data, mountPath: /data }]
  volumes:
    - name: data
      persistentVolumeClaim: { claimName: ${PVC} }
EOF
$K wait --for=condition=Ready "pod/${HELPER}" --timeout=180s >/dev/null

echo "==> Copying BinaryData/ ($(du -sh "$SRC/BinaryData" | cut -f1))"
tar -C "$SRC" -cf - BinaryData | $K exec -i "$HELPER" -- sh -c 'rm -rf /data/BinaryData && tar -C /data -xf -'

echo "==> Copying Map/Zone/Model/*.qmp ($(ls "$SRC/Map/Zone/Model"/*.qmp | wc -l) files)"
# Only the .qmp files: the client's Map tree is 1.4GB of textures we never read.
( cd "$SRC" && find Map/Zone/Model -name '*.qmp' ) \
  | tar -C "$SRC" -cf - -T - | $K exec -i "$HELPER" -- sh -c 'rm -rf /data/Map && tar -C /data -xf -'

echo "==> On the PVC:"
$K exec "$HELPER" -- sh -c 'du -sh /data/BinaryData /data/Map; ls /data/BinaryData'
echo "==> Done. Restart the server to pick it up: kubectl -n ${NAMESPACE} rollout restart deployment/${RELEASE}"
