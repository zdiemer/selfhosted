#!/usr/bin/env bash
# The verification gate from README.md, as something you can actually run twice.
#
#   ./verify.sh                       both classes
#   ./verify.sh --class truenas-nfs   one class
#   ./verify.sh --keep                leave the smoke volumes behind to poke at
#
# Proves the whole round trip on throwaway volumes: provision, write, snapshot,
# restore, compare, expand. All five must pass on both classes before anything
# real depends on a change to this chart.
#
# WHY A SCRIPT AND NOT A CHECKLIST. The gate was written for the original
# migration, as prose, on the assumption it runs once. It does not: every driver
# bump is a reason to run it again, and a gate that is tedious to run is a gate
# that gets skipped precisely when it matters. Expansion in particular is the
# step most likely to be quietly broken and the one local-path never had.
#
# Everything lives in its own namespace and is deleted at the end. The classes
# are reclaimPolicy: Delete, so the zvols and datasets go with the PVCs.

set -euo pipefail

NS="${NS:-csi-smoke}"
CLASSES=(truenas-iscsi truenas-nfs)
KEEP=false

while [[ "${1:-}" != "" ]]; do
    case "$1" in
        --class) CLASSES=("$2"); shift 2 ;;
        --keep)  KEEP=true; shift ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Error: unknown option: $1" >&2; exit 1 ;;
    esac
done

command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

K="kubectl -n ${NS}"
FAILED=0
# Deterministic payload, big enough that a truncated restore is obvious and
# small enough to write instantly.
PAYLOAD='democratic-csi smoke test payload'

cleanup() {
    [[ "$KEEP" == "true" ]] && { echo "--keep: leaving namespace ${NS}"; return; }
    echo ""
    echo "==> cleaning up ${NS}"
    # The namespace delete cascades to PVCs, and reclaimPolicy Delete takes the
    # zvols/datasets with them. Backgrounded --wait=false would race the script
    # exit and leave volumes on the NAS, so this blocks.
    kubectl delete namespace "$NS" --ignore-not-found --timeout=300s >/dev/null 2>&1 || true
}
trap cleanup EXIT

step() { printf '    %-34s' "$1"; }
ok()   { echo "[OK] ${1:-}"; }
bad()  { echo "[FAIL] ${1:-}"; FAILED=1; }

kubectl get namespace "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS" >/dev/null

for SC in "${CLASSES[@]}"; do
    echo ""
    echo "=== ${SC} ==="
    SRC="smoke-src"; RESTORE="smoke-restore"; SNAP="smoke-snap"
    # One namespace per class would be tidier but much slower to tear down;
    # instead each class reuses the same names and is cleaned between runs.
    $K delete pvc "$SRC" "$RESTORE" --ignore-not-found --timeout=180s >/dev/null 2>&1 || true
    $K delete volumesnapshot "$SNAP" --ignore-not-found --timeout=180s >/dev/null 2>&1 || true
    $K delete pod smoke-writer smoke-reader smoke-keeper --ignore-not-found --timeout=120s >/dev/null 2>&1 || true

    # ---------------------------------------------------------------- 1. bind
    step "1. provision 1Gi"
    cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: $SRC, namespace: $NS }
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: $SC
  resources: { requests: { storage: 1Gi } }
EOF
    if $K wait --for=jsonpath='{.status.phase}'=Bound "pvc/$SRC" --timeout=180s >/dev/null 2>&1; then
        ok "$($K get pvc "$SRC" -o jsonpath='{.spec.volumeName}')"
    else
        bad "never reached Bound"; continue
    fi

    # ---------------------------------------------------------------- 2. write
    step "2. write + fsync"
    cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: { name: smoke-writer, namespace: $NS }
spec:
  restartPolicy: Never
  containers:
    - name: w
      image: busybox:1.37.0
      command: ["sh","-ec","printf '%s' \"\$PAYLOAD\" > /data/canary; sync; md5sum /data/canary"]
      env: [{ name: PAYLOAD, value: "$PAYLOAD" }]
      volumeMounts: [{ name: v, mountPath: /data }]
  volumes: [{ name: v, persistentVolumeClaim: { claimName: $SRC } }]
EOF
    if $K wait --for=jsonpath='{.status.phase}'=Succeeded pod/smoke-writer --timeout=180s >/dev/null 2>&1; then
        SUM_SRC=$($K logs smoke-writer | awk '{print $1}')
        ok "md5 ${SUM_SRC:0:12}…"
    else
        bad "writer pod did not succeed"; $K logs smoke-writer 2>&1 | tail -5; continue
    fi

    # ------------------------------------------------------------- 3. snapshot
    step "3. snapshot"
    cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata: { name: $SNAP, namespace: $NS }
spec:
  volumeSnapshotClassName: $SC
  source: { persistentVolumeClaimName: $SRC }
EOF
    if $K wait --for=jsonpath='{.status.readyToUse}'=true "volumesnapshot/$SNAP" --timeout=300s >/dev/null 2>&1; then
        ok
    else
        bad "snapshot never became readyToUse"
        $K get volumesnapshot "$SNAP" -o jsonpath='{.status}' 2>/dev/null | head -c 300; echo
        continue
    fi

    # -------------------------------------------------------------- 4. restore
    step "4. restore into new PVC"
    cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: $RESTORE, namespace: $NS }
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: $SC
  dataSource: { name: $SNAP, kind: VolumeSnapshot, apiGroup: snapshot.storage.k8s.io }
  resources: { requests: { storage: 1Gi } }
EOF
    if $K wait --for=jsonpath='{.status.phase}'=Bound "pvc/$RESTORE" --timeout=300s >/dev/null 2>&1; then
        ok
    else
        bad "restored PVC never bound"; continue
    fi

    # -------------------------------------------------------------- 5. compare
    step "5. compare restored contents"
    cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: { name: smoke-reader, namespace: $NS }
spec:
  restartPolicy: Never
  containers:
    - name: r
      image: busybox:1.37.0
      command: ["sh","-ec","md5sum /data/canary"]
      volumeMounts: [{ name: v, mountPath: /data }]
  volumes: [{ name: v, persistentVolumeClaim: { claimName: $RESTORE } }]
EOF
    if $K wait --for=jsonpath='{.status.phase}'=Succeeded pod/smoke-reader --timeout=180s >/dev/null 2>&1; then
        SUM_RESTORE=$($K logs smoke-reader | awk '{print $1}')
        if [[ "$SUM_RESTORE" == "$SUM_SRC" ]]; then
            ok "matches source"
        else
            bad "checksum mismatch: $SUM_SRC vs $SUM_RESTORE"
        fi
    else
        bad "reader pod did not succeed"; $K logs smoke-reader 2>&1 | tail -5
    fi

    # --------------------------------------------------------------- 6. expand
    # Filesystem expansion only completes while the volume is MOUNTED — an
    # offline PVC will sit at FileSystemResizePending forever and look like a
    # driver bug. So a keeper pod holds it open across the resize.
    step "6. expand 1Gi -> 2Gi"
    cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: { name: smoke-keeper, namespace: $NS }
spec:
  containers:
    - name: k
      image: busybox:1.37.0
      command: ["sh","-c","sleep 3600"]
      volumeMounts: [{ name: v, mountPath: /data }]
  volumes: [{ name: v, persistentVolumeClaim: { claimName: $SRC } }]
EOF
    if ! $K wait --for=condition=ready pod/smoke-keeper --timeout=180s >/dev/null 2>&1; then
        bad "keeper pod never became ready"; continue
    fi
    $K patch pvc "$SRC" --type=merge \
        -p '{"spec":{"resources":{"requests":{"storage":"2Gi"}}}}' >/dev/null
    if $K wait --for=jsonpath='{.status.capacity.storage}'=2Gi "pvc/$SRC" --timeout=300s >/dev/null 2>&1; then
        INSIDE=$($K exec smoke-keeper -- df -hP /data 2>/dev/null | awk 'NR==2{print $2}')
        ok "PVC 2Gi, filesystem reports ${INSIDE:-?}"
    else
        bad "capacity stuck at $($K get pvc "$SRC" -o jsonpath='{.status.capacity.storage}')"
        $K get pvc "$SRC" -o jsonpath='{.status.conditions[*].type}' 2>/dev/null; echo
    fi
done

echo ""
if [[ "$FAILED" == "0" ]]; then
    echo "=== PASS — provision, write, snapshot, restore, compare, expand on: ${CLASSES[*]} ==="
else
    echo "=== FAIL — see above. Nothing real should depend on this driver build. ==="
fi
exit "$FAILED"
