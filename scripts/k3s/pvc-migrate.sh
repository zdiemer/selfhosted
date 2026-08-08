#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

# Migrate one PVC from local-path to a TrueNAS-backed StorageClass.
#
#   ./pvc-migrate.sh <namespace> <pvc> <target-class>
#
# 20 volumes is too many for hand-written runbooks, so the runbook is this
# script and it runs 20 times.
#
# SAFETY MODEL — the source data is never deleted by this script.
#
#   * It refuses to start unless the source PV is already Retain
#     (scripts/k3s/pv-retain-guard.sh --apply).
#   * Deleting the PVC therefore leaves the PV Released with the directory
#     intact on the node, which is the rollback.
#   * The copy is verified with a checksum-level rsync diff, not just a byte
#     count, and the script aborts before bringing anything up if it disagrees.
#   * The old PV is left Released and Retain when the script finishes. Reclaiming
#     it is a separate, later, deliberate act — scripts/k3s/cleanup.sh reports
#     the orphans.
#
# Default mode is --plan: it inspects and prints, and changes nothing. You have
# to pass --execute to move data.

PLAN_ONLY=true
ASSUME_YES=false
CHART_CMD=""
TARGET_SIZE=""
RSYNC_IMAGE="${RSYNC_IMAGE:-instrumentisto/rsync-ssh:alpine}"

usage() {
    cat <<EOF
Usage: $(basename "$0") <namespace> <pvc> <target-class> [options]

Migrate a PVC from local-path to truenas-iscsi or truenas-nfs, preserving data.

OPTIONS:
  --execute            Actually perform the migration (default: plan only)
  --yes                Don't prompt at each destructive step (implies --execute)
  --target-size <size> Size the chart will give the NEW claim, e.g. 4Gi. Only
                       needed when the migration also RAISES the size: the
                       capacity check otherwise compares against the old claim's
                       declaration and refuses a volume that has outgrown it.
  --chart-cmd <cmd>    Command that recreates the PVC on the new class, usually
                       the chart's upgrade.sh. Run automatically at step 5
                       instead of pausing for you to do it by hand. Required
                       with --yes, since there is otherwise nothing to recreate
                       the claim before the copy looks for it.
  -h, --help           Show this help

EXAMPLES:
  $(basename "$0") docs stirling-configs truenas-nfs
  $(basename "$0") docs stirling-configs truenas-nfs --execute

MIGRATION ORDER (see the approved plan):
  wave 1  stirling-configs, gamedex-data          <- rehearse here
  wave 2  keepass, sms-relay, apartment-watch, money, happy-server, whatnow
  wave 3  paperless-postgres, romm-db, vocard-mongo   <- dump first
  wave 4  paperless-data, romm-data, claude-workspace-home, smitele-bot-data
  wave 5  mc-data, mc-backups                     <- offline window, players out

TWO CASES THIS SCRIPT CANNOT HANDLE:
  * discord/vocard mongo — StatefulSet volumeClaimTemplates are immutable.
    Needs 'kubectl delete statefulset --cascade=orphan' then a helm upgrade.
  * minecraft mc-data / mc-backups — existingClaim, so the chart will not
    recreate the PVC. Create the target claim by hand first.
EOF
    exit 0
}

[[ $# -lt 3 ]] && usage
NAMESPACE="$1"; PVC="$2"; TARGET_CLASS="$3"; shift 3

while [[ "${1:-}" != "" ]]; do
    case "$1" in
        --execute) PLAN_ONLY=false; shift ;;
        --yes)     PLAN_ONLY=false; ASSUME_YES=true; shift ;;
        --chart-cmd) CHART_CMD="$2"; shift 2 ;;
        --target-size) TARGET_SIZE="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Error: Unknown option: $1" >&2; usage ;;
    esac
done

require_tools kubectl python3

# --yes silences the step-5 prompt too, so without a chart command there is
# nothing to recreate the claim and the copy would race a PVC that never
# appears. Refuse the combination rather than fail halfway through.
if [[ "$ASSUME_YES" == "true" && -z "$CHART_CMD" ]]; then
    echo "Error: --yes requires --chart-cmd." >&2
    echo "       Otherwise nothing recreates pvc/${PVC} between the delete and the copy." >&2
    exit 1
fi

K="kubectl -n ${NAMESPACE}"
TMP_CLAIM="${PVC}-migsrc"
COPY_JOB="${PVC}-migcopy"

confirm() {
    [[ "$ASSUME_YES" == "true" ]] && return 0
    local msg="$1"
    echo ""
    read -rp ">>> ${msg} [y/N] " ans
    [[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "Aborted."; exit 1; }
}

step() { echo ""; echo "=== $* ==="; }

# ------------------------------------------------------------------------------
# 1. Inspect
# ------------------------------------------------------------------------------
step "1. Source"

$K get pvc "$PVC" >/dev/null 2>&1 || { echo "FAIL: pvc/${PVC} not found in ${NAMESPACE}"; exit 1; }

SRC_PV="$($K get pvc "$PVC" -o jsonpath='{.spec.volumeName}')"
SRC_CLASS="$($K get pvc "$PVC" -o jsonpath='{.spec.storageClassName}')"
SIZE="$($K get pvc "$PVC" -o jsonpath='{.spec.resources.requests.storage}')"
MODE="$($K get pvc "$PVC" -o jsonpath='{.spec.accessModes[0]}')"
POLICY="$(kubectl get pv "$SRC_PV" -o jsonpath='{.spec.persistentVolumeReclaimPolicy}')"
SRC_NODE="$(kubectl get pv "$SRC_PV" \
    -o jsonpath='{.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]}' 2>/dev/null || true)"

printf '  %-14s %s\n' pvc "${NAMESPACE}/${PVC}"
printf '  %-14s %s\n' pv "$SRC_PV"
printf '  %-14s %s -> %s\n' class "$SRC_CLASS" "$TARGET_CLASS"
printf '  %-14s %s (%s)\n' size "$SIZE" "$MODE"
printf '  %-14s %s\n' 'reclaim' "$POLICY"
printf '  %-14s %s\n' 'pinned to' "${SRC_NODE:-<none>}"

kubectl get storageclass "$TARGET_CLASS" >/dev/null 2>&1 \
    || { echo "FAIL: storageclass/${TARGET_CLASS} does not exist"; exit 1; }

# The whole safety model rests on this. Without Retain, deleting the PVC in
# step 4 destroys the data outright.
if [[ "$POLICY" != "Retain" ]]; then
    echo ""
    echo "FAIL: pv/${SRC_PV} is reclaimPolicy=${POLICY}."
    echo "      Deleting the PVC would destroy the data with no rollback."
    echo "      Run:  ./scripts/k3s/pv-retain-guard.sh --apply"
    exit 1
fi

# ------------------------------------------------------------------------------
# 1b. Will the data actually fit?
# ------------------------------------------------------------------------------
# local-path enforces NO quota, so a claim's declared size is fiction — several
# volumes here hold far more than they ask for (talaria's postgres declares 1Gi
# and holds 75G). Both truenas classes enforce their size for real, so migrating
# at the declared size fails mid-copy or truncates. Measure the source before
# touching anything.
step "1b. Capacity"

SRC_PATH="$(kubectl get pv "$SRC_PV" -o jsonpath='{.spec.hostPath.path}' 2>/dev/null)"
[[ -z "$SRC_PATH" ]] && SRC_PATH="$(kubectl get pv "$SRC_PV" -o jsonpath='{.spec.local.path}' 2>/dev/null)"

ACTUAL_BYTES=""
if [[ -n "$SRC_NODE" && -n "$SRC_PATH" ]]; then
    ACTUAL_BYTES="$(run_on_node_sudo "$SRC_NODE" "du -sb '$SRC_PATH' 2>/dev/null | cut -f1" 2>/dev/null | tr -d '[:space:]' || true)"
fi

if [[ -z "$ACTUAL_BYTES" || ! "$ACTUAL_BYTES" =~ ^[0-9]+$ ]]; then
    echo "  [WARN] could not measure ${SRC_NODE}:${SRC_PATH}"
    echo "         Verify by hand that the data fits in ${SIZE} before continuing."
else
    CHECK_SIZE="${TARGET_SIZE:-$SIZE}"
    REQ_BYTES="$(python3 -c "
import re,sys
s=${CHECK_SIZE@Q}
m=re.match(r'([0-9.]+)([KMGTPEi]*)',s)
u={'Ki':1024,'Mi':1024**2,'Gi':1024**3,'Ti':1024**4,'K':1000,'M':1000**2,'G':1000**3,'T':1000**4,'':1}
print(int(float(m.group(1))*u.get(m.group(2),1)))
")"
    printf '  %-14s %s%s\n' 'declared' "$SIZE" "${TARGET_SIZE:+  -> ${TARGET_SIZE} (target)}"
    printf '  %-14s %s\n' 'actual' "$(numfmt --to=iec-i --suffix=B "$ACTUAL_BYTES" 2>/dev/null || echo "${ACTUAL_BYTES}B")"
    # 10% headroom: ext4 metadata and the filesystem's own overhead mean a
    # volume sized exactly to its contents has nowhere to land.
    NEED=$(( ACTUAL_BYTES * 110 / 100 ))
    if [[ "$NEED" -gt "$REQ_BYTES" ]]; then
        echo ""
        echo "FAIL: the source holds more than the target claim will allow."
        echo "      needs at least $(numfmt --to=iec-i --suffix=B "$NEED" 2>/dev/null || echo "${NEED}B") including 10% headroom."
        echo ""
        echo "      local-path enforces no quota, so this volume outgrew its own"
        echo "      declaration without anything complaining. ${TARGET_CLASS} does"
        echo "      enforce it, and the copy would fail partway."
        echo ""
        echo "      Raise persistence.size in the chart, then re-run with"
        echo "        --target-size <newsize>"
        echo "      so this check measures against the claim the chart will create,"
        echo "      not the old declaration it has already outgrown."
        exit 1
    fi
    echo "  [OK] fits, with $(( (REQ_BYTES - ACTUAL_BYTES) * 100 / REQ_BYTES ))% of the claim spare"
fi

# ------------------------------------------------------------------------------
# 2. Consumers
# ------------------------------------------------------------------------------
step "2. Workloads using this PVC"

# Resolve pods -> their controllers, collapsing ReplicaSet to its Deployment so
# scaling actually sticks.
mapfile -t OWNERS < <($K get pods -o json | python3 -c "
import json, subprocess, sys
pvc = ${PVC@Q}
ns  = ${NAMESPACE@Q}
d = json.load(sys.stdin)
out = set()
for p in d['items']:
    vols = p['spec'].get('volumes') or []
    if not any((v.get('persistentVolumeClaim') or {}).get('claimName') == pvc for v in vols):
        continue
    refs = p['metadata'].get('ownerReferences') or []
    if not refs:
        out.add('pod/' + p['metadata']['name']); continue
    for r in refs:
        if r['kind'] == 'ReplicaSet':
            rs = json.loads(subprocess.run(
                ['kubectl','-n',ns,'get','rs',r['name'],'-o','json'],
                capture_output=True, text=True).stdout or '{}')
            dep = (rs.get('metadata',{}).get('ownerReferences') or [{}])[0]
            if dep.get('kind') == 'Deployment':
                out.add('deployment/' + dep['name']); continue
            out.add('replicaset/' + r['name'])
        elif r['kind'] == 'Job':
            out.add('job/' + r['name'])
        else:
            out.add(r['kind'].lower() + '/' + r['name'])
print('\n'.join(sorted(out)))
")

# CronJobs never have a running pod between fires, so find them by spec instead.
mapfile -t CRONJOBS < <($K get cronjobs -o json 2>/dev/null | python3 -c "
import json, sys
pvc = ${PVC@Q}
try: d = json.load(sys.stdin)
except Exception: sys.exit()
for c in d.get('items', []):
    vols = c['spec']['jobTemplate']['spec']['template']['spec'].get('volumes') or []
    if any((v.get('persistentVolumeClaim') or {}).get('claimName') == pvc for v in vols):
        print('cronjob/' + c['metadata']['name'])
")

if [[ ${#OWNERS[@]} -eq 0 && ${#CRONJOBS[@]} -eq 0 ]]; then
    echo "  (none running — nothing to scale down)"
else
    printf '  %s\n' "${OWNERS[@]}" "${CRONJOBS[@]}" 2>/dev/null | grep -v '^  $' || true
fi

# Record replica counts so they can be restored exactly, rather than assumed to
# be 1. A workload that was deliberately scaled to 3 must come back at 3.
declare -A REPLICAS=()
for o in ${OWNERS[@]+"${OWNERS[@]}"}; do
    [[ "$o" == pod/* || "$o" == job/* ]] && continue
    REPLICAS["$o"]="$($K get "$o" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 1)"
    # Step 8 restores exactly what is recorded here. If a workload is ALREADY at
    # zero — because it was scaled down by hand before running this — then zero
    # is what it gets restored to, and the migration finishes "successfully"
    # with the app still down. Say so now rather than leave it a surprise.
    if [[ "${REPLICAS[$o]}" == "0" ]]; then
        echo "  [WARN] $o is already at 0 replicas."
        echo "         It will be RESTORED to 0 — scale it up yourself afterwards."
    fi
done

if [[ "$PLAN_ONLY" == "true" ]]; then
    echo ""
    echo "=== PLAN ONLY — nothing has changed ==="
    echo ""
    echo "With --execute this would:"
    echo "  1. scale the workloads above to 0 and wait for pods to terminate"
    echo "  2. delete pvc/${PVC}  (pv/${SRC_PV} -> Released, data intact)"
    echo "  3. pause for you to run the chart's upgrade.sh with"
    echo "       persistence.storageClass: ${TARGET_CLASS}"
    echo "  4. re-bind pv/${SRC_PV} read-only via a temp claim '${TMP_CLAIM}'"
    echo "  5. run an rsync Job copying old -> new"
    echo "  6. verify with a checksum-level rsync diff (must be empty)"
    echo "  7. restore replica counts and report the new pod's node"
    echo ""
    echo "The old PV is left Released+Retain as the rollback."
    exit 0
fi

# ------------------------------------------------------------------------------
# 3. Scale down
# ------------------------------------------------------------------------------
scale_everything_down() {
    # Every step here reports what it did. An earlier silent version appeared to
    # hang for 300s on claude-bridge with the workload still at 1 replica and no
    # way to see which part had not taken effect. A scale-down that quietly
    # no-ops is indistinguishable from one that worked, right up until the copy
    # Job cannot mount the volume.
    local o cj rc
    for cj in ${CRONJOBS[@]+"${CRONJOBS[@]}"}; do
        [[ -z "$cj" ]] && continue
        if $K patch "$cj" -p '{"spec":{"suspend":true}}' >/dev/null 2>&1; then
            echo "    suspended $cj"
        else
            echo "    [WARN] could not suspend $cj"
        fi
    done
    for o in ${OWNERS[@]+"${OWNERS[@]}"}; do
        [[ -z "$o" || "$o" == pod/* || "$o" == job/* ]] && continue
        rc=0
        $K scale "$o" --replicas=0 >/dev/null 2>&1 || rc=$?
        if [[ "$rc" != "0" ]]; then
            echo "    [ERROR] 'kubectl scale $o --replicas=0' failed (rc=$rc)"
            return 1
        fi
        # Trust nothing: read the spec back. A scale that reports success but
        # leaves replicas>0 means something else owns this field.
        local want
        want="$($K get "$o" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo '?')"
        echo "    scaled $o -> ${want}"
        if [[ "$want" != "0" ]]; then
            echo "    [ERROR] $o is still at ${want} replicas after scaling to 0."
            echo "            Something is reconciling it — an operator, a KEDA"
            echo "            ScaledObject, or a helm release re-applying behind us."
            return 1
        fi
    done

    local remaining=""
    for _ in $(seq 1 60); do
        remaining="$($K get pods -o json | python3 -c "
import json, sys
pvc = ${PVC@Q}
d = json.load(sys.stdin)
print(sum(1 for p in d['items']
          if p.get('status', {}).get('phase') not in ('Succeeded', 'Failed')
          and any((v.get('persistentVolumeClaim') or {}).get('claimName') == pvc
                  for v in (p['spec'].get('volumes') or []))))
")"
        [[ "$remaining" == "0" ]] && return 0
        sleep 5
    done
    echo "FAIL: ${remaining} live pod(s) still hold the PVC"
    return 1
}

step "3. Scale down"
confirm "Scale down ${#OWNERS[@]} workload(s) in ${NAMESPACE}?"

for cj in "${CRONJOBS[@]}"; do
    [[ -z "$cj" ]] && continue
    echo "  suspending $cj"
    $K patch "$cj" -p '{"spec":{"suspend":true}}' >/dev/null
done

for o in "${OWNERS[@]}"; do
    [[ -z "$o" ]] && continue
    case "$o" in
        pod/*)
            echo "  deleting bare $o"
            $K delete "$o" --wait=false >/dev/null
            ;;
        job/*)
            # A Job cannot be scaled. Deleting it is safe for the CronJob-spawned
            # ones here — the CronJob (suspended above) re-fires later. Completed
            # Jobs are deleted too so they stop referencing the claim.
            echo "  deleting $o"
            $K delete "$o" --wait=false >/dev/null
            ;;
        *)
            echo "  scaling $o to 0 (was ${REPLICAS[$o]:-?})"
            $K scale "$o" --replicas=0 >/dev/null
            ;;
    esac
done

echo "  waiting for pods to terminate..."
for _ in $(seq 1 60); do
    # Succeeded/Failed pods still list the PVC in their spec but hold no mount —
    # web/apartment-watch keeps several completed CronJob pods around, and
    # counting those would never reach zero. Only live pods block a migration.
    remaining="$($K get pods -o json | python3 -c "
import json, sys
pvc = ${PVC@Q}
d = json.load(sys.stdin)
print(sum(1 for p in d['items']
          if p.get('status', {}).get('phase') not in ('Succeeded', 'Failed')
          and any((v.get('persistentVolumeClaim') or {}).get('claimName') == pvc
                  for v in (p['spec'].get('volumes') or []))))
")"
    [[ "$remaining" == "0" ]] && break
    sleep 5
done
[[ "$remaining" == "0" ]] || { echo "FAIL: ${remaining} live pod(s) still hold the PVC"; exit 1; }
echo "  [OK] no live pods hold the volume"

# ------------------------------------------------------------------------------
# 4. Release the source PV
# ------------------------------------------------------------------------------
step "4. Release source PV"
confirm "Delete pvc/${PVC}? (pv/${SRC_PV} is Retain — data survives)"

$K delete pvc "$PVC" --wait=true
# Re-point claimRef at the temp claim rather than clearing it to null.
#
# Null would work — the temp claim would bind — but it also opens a window from
# here until step 6 in which ANY PVC of this class and size can take the source
# volume. Step 5 sits inside that window and creates a PVC by design, so a chart
# that has not actually been switched to the new class (a values edit that
# silently did not apply, say) can bind straight onto the data being migrated.
#
# Naming the temp claim up front means only that claim can ever bind it.
# uid and resourceVersion MUST be nulled explicitly. A merge patch merges into
# the existing claimRef object, so setting only name/namespace leaves the old
# claim's uid behind and the binder rejects the new claim with "already bound to
# a different claim" — the PV sits Released forever.
kubectl patch pv "$SRC_PV" --type=merge \
    -p "{\"spec\":{\"claimRef\":{\"namespace\":\"${NAMESPACE}\",\"name\":\"${TMP_CLAIM}\",\"uid\":null,\"resourceVersion\":null}}}" >/dev/null
echo "  [OK] pv/${SRC_PV} is $(kubectl get pv "$SRC_PV" -o jsonpath='{.status.phase}')"

# ------------------------------------------------------------------------------
# 5. Recreate the PVC on the new class
# ------------------------------------------------------------------------------
step "5. Recreate the claim on ${TARGET_CLASS}"
if [[ -n "$CHART_CMD" ]]; then
    echo "  running: ${CHART_CMD}"
    if ! bash -c "$CHART_CMD"; then
        echo ""
        echo "FAIL: chart command exited non-zero."
        echo "      pvc/${PVC} was deleted but pv/${SRC_PV} is Retain and intact."
        echo "      Fix the chart, then re-run this script from step 5."
        exit 1
    fi
else
    cat <<EOF

  Now run the chart's upgrade.sh with the new storage class, e.g.

      persistence.storageClass: ${TARGET_CLASS}

  The chart will recreate pvc/${PVC}, empty, on the NAS. Leave the workload
  scaled at 0 — this script restores the replica counts at the end.

EOF
    confirm "Has pvc/${PVC} been recreated on ${TARGET_CLASS}?"
fi

NEW_CLASS="$($K get pvc "$PVC" -o jsonpath='{.spec.storageClassName}' 2>/dev/null || true)"
[[ "$NEW_CLASS" == "$TARGET_CLASS" ]] \
    || { echo "FAIL: pvc/${PVC} is on '${NEW_CLASS:-<missing>}', expected ${TARGET_CLASS}"; exit 1; }
$K wait --for=jsonpath='{.status.phase}'=Bound "pvc/${PVC}" --timeout=120s
echo "  [OK] pvc/${PVC} bound on ${TARGET_CLASS}"

# The chart just re-applied its own manifests, which resets replicas to whatever
# values.yaml says — so the workload is running again, against a brand new EMPTY
# volume. Left alone it would serve empty state to users and fight the copy Job
# for the RWO claim. Put it back to zero before anything touches the data.
echo "  re-scaling to 0 after the chart upgrade"
scale_everything_down || exit 1
echo "  [OK] volume is idle again"

# ------------------------------------------------------------------------------
# 6. Copy
# ------------------------------------------------------------------------------
step "6. Copy data"

# Temp claim pinned to the released PV by volumeName. Same pattern as
# games/romm/templates/library-pvc.yaml.
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${TMP_CLAIM}
  namespace: ${NAMESPACE}
spec:
  accessModes: [${MODE}]
  # The SOURCE class, not "". Binding requires PVC.storageClassName to equal
  # PV.storageClassName exactly, and a local-path PV carries "local-path" —
  # "" means "no class" and the controller rejects it with VolumeMismatch.
  # (The "" idiom in games/romm's static SMB PVs works only because those PVs
  # genuinely declare "" themselves.)
  storageClassName: ${SRC_CLASS}
  volumeName: ${SRC_PV}
  resources:
    requests:
      storage: ${SIZE}
EOF
$K wait --for=jsonpath='{.status.phase}'=Bound "pvc/${TMP_CLAIM}" --timeout=120s
echo "  [OK] temp claim bound to the old data"

# No nodeSelector needed: the source PV's own nodeAffinity pins this Job to the
# right node automatically.
$K delete job "$COPY_JOB" --ignore-not-found >/dev/null
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${COPY_JOB}
  namespace: ${NAMESPACE}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: copy
          image: ${RSYNC_IMAGE}
          command: [sh, -ceu]
          args:
            - |
              echo "--- source ---"
              # NOTE: this manifest is an UNQUOTED heredoc so it can interpolate
              # \${PVC}, \${COPY_JOB} and friends. Every variable meant for the
              # POD's shell must therefore be escaped, or the local shell eats it
              # — an unescaped \$(find /src ...) runs on the workstation, where
              # /src does not exist.
              du -sh /src; SRCN=\$(find /src -type f | wc -l); echo "\$SRCN files"
              echo "--- copying ---"
              # --delete makes the destination an exact mirror. The chart upgrade
              # in step 5 briefly runs the app against the new EMPTY volume, and
              # it writes things there: stirling generated a fresh JWT signing
              # keypair and an empty-DB backup in 76 seconds. Those are artifacts
              # of that boot, not data, and without --delete they survive the
              # migration and shadow the real state.
              rsync -aHAX --delete --numeric-ids --info=progress2 /src/ /dst/
              echo "--- verify (must be empty) ---"
              # --delete here too. Without it rsync only reports files present in
              # the source, so anything existing ONLY in the destination passes
              # silently — which is exactly the case this needs to catch.
              rsync -aHAXn --delete --checksum --itemize-changes /src/ /dst/ > /tmp/diff
              if [ -s /tmp/diff ]; then
                echo "MISMATCH:"; cat /tmp/diff; exit 1
              fi
              echo "--- destination ---"
              du -sh /dst; DSTN=\$(find /dst -type f | wc -l); echo "\$DSTN files"
              # Belt and braces: the checksum pass above should make this
              # impossible, but a count mismatch is cheap to assert and would
              # catch anything rsync's filters skipped.
              if [ "\$SRCN" != "\$DSTN" ]; then
                echo "FILE COUNT MISMATCH: src=\$SRCN dst=\$DSTN"; exit 1
              fi
              echo "VERIFIED"
          volumeMounts:
            - { name: src, mountPath: /src, readOnly: true }
            - { name: dst, mountPath: /dst }
      volumes:
        - name: src
          persistentVolumeClaim: { claimName: ${TMP_CLAIM}, readOnly: true }
        - name: dst
          persistentVolumeClaim: { claimName: ${PVC} }
EOF

echo "  copying (follow with: kubectl -n ${NAMESPACE} logs -f job/${COPY_JOB})"
if ! $K wait --for=condition=complete "job/${COPY_JOB}" --timeout=3600s 2>/dev/null; then
    echo ""
    echo "FAIL: copy job did not complete. Logs:"
    $K logs "job/${COPY_JOB}" --tail=50 || true
    echo ""
    echo "The source data is untouched on pv/${SRC_PV}."
    echo "Nothing has been lost. Investigate, then re-run from step 6."
    exit 1
fi

$K logs "job/${COPY_JOB}" --tail=20

# ------------------------------------------------------------------------------
# 7. Verify + restore
# ------------------------------------------------------------------------------
step "7. Verify"
$K logs "job/${COPY_JOB}" | grep -q '^VERIFIED$' \
    || { echo "FAIL: copy job finished without VERIFIED"; exit 1; }
echo "  [OK] checksum-level diff was empty"

$K delete job "$COPY_JOB" --ignore-not-found >/dev/null
$K delete pvc "$TMP_CLAIM" --ignore-not-found >/dev/null

# RESERVE the rollback PV — do NOT clear claimRef.
#
# An earlier version cleared it so the PV would read Available rather than
# Released. That put the rollback back into the free pool, and a PV with no
# claimRef is fair game for any PVC of the same class and size. It bit
# immediately: the next chart to create a local-path claim (web/apartment-watch)
# bound straight onto finance/money-data's rollback volume, mounting one app's
# database into another. Nothing was lost only because apartment-watch opens
# SQLite read-only and found no file of its own.
#
# Pointing claimRef at a name that will never exist reserves the PV forever: the
# binder only ever matches a PV to the exact namespace/name in its claimRef.
kubectl patch pv "$SRC_PV" --type=merge \
    -p "{\"spec\":{\"claimRef\":{\"namespace\":\"${NAMESPACE}\",\"name\":\"${PVC}-ROLLBACK-DO-NOT-BIND\",\"uid\":null,\"resourceVersion\":null}}}" \
    >/dev/null 2>&1 || true

step "8. Scale back up"
for o in "${OWNERS[@]}"; do
    [[ -z "$o" || "$o" == pod/* || "$o" == job/* ]] && continue
    echo "  scaling $o to ${REPLICAS[$o]:-1}"
    $K scale "$o" --replicas="${REPLICAS[$o]:-1}" >/dev/null
done
for cj in "${CRONJOBS[@]}"; do
    [[ -z "$cj" ]] && continue
    echo "  unsuspending $cj"
    $K patch "$cj" -p '{"spec":{"suspend":false}}' >/dev/null
done

sleep 10
step "Done"
$K get pods -o wide | head -20
echo ""
echo "Source data is still on pv/${SRC_PV} (Retain, unbound) as the rollback."
echo "Do NOT reclaim it until this workload has run cleanly for a while and a"
echo "restore has been verified. scripts/k3s/cleanup.sh reports orphaned PV dirs."
if [[ -n "$SRC_NODE" ]]; then
    echo ""
    echo "This volume was pinned to ${SRC_NODE}. Prove it is free now:"
    echo "  kubectl drain ${SRC_NODE} --ignore-daemonsets --delete-emptydir-data"
    echo "  kubectl uncordon ${SRC_NODE}"
fi
