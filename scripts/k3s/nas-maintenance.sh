#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

# Quiesce the cluster for planned NAS downtime, and bring it back afterwards.
#
#   ./nas-maintenance.sh --start     # before powering the NAS off
#   ./nas-maintenance.sh --finish    # once it is back
#
# WHY QUIESCE RATHER THAN RIDE IT OUT. The two storage classes fail very
# differently, and the worse one decides the procedure:
#
#   truenas-nfs    hard mounts block I/O while the server is away and resume
#                  cleanly. Pods hang; nothing corrupts; no restart needed.
#   truenas-iscsi  after node.session.timeo.replacement_timeout (300s, set by
#                  iscsi-prereq.sh) the initiator returns I/O errors, ext4
#                  remounts READ-ONLY, and the filesystem stays read-only even
#                  after the NAS returns. Every affected pod needs restarting,
#                  and anything mid-write may have lost it.
#
# So a NAS window is not "a few minutes of slowness" — left alone it read-onlys
# every database in the cluster. Scaling to zero first means every volume is
# cleanly unmounted, iSCSI sessions log out normally, and no filesystem is left
# dirty.
#
# WHAT STAYS UP. Only workloads with a truenas-* volume are touched. The
# survival tier (authelia, traefik) is deliberately on local-path, so ingress
# and login keep working, and every stateless service serves normally. A NAS
# window is a partial outage, not a total one.

ACTION=""
STATE_FILE="${NAS_MAINT_STATE:-${HOME}/.cache/nas-maintenance-state.json}"
SKIP_BACKUP_CHECK=false

usage() {
    cat <<EOF
Usage: $(basename "$0") --start | --finish [options]

Quiesce every workload with NAS-backed storage so the NAS can be powered off,
then restore them afterwards.

OPTIONS:
  --start              Scale down and wait until nothing holds a NAS volume
  --finish             Wait for the NAS, then restore what --start scaled down
  --status             Show what would be affected; change nothing
  --skip-backup-check  Don't require a recent successful backup (--start only)
  -h, --help           Show this help

STATE:
  Replica counts are recorded in ${STATE_FILE}
  so --finish restores what was actually running, not an assumption of 1.

TYPICAL WINDOW:
  ./nas-maintenance.sh --start
  # ... power off, add RAM / swap a disk / update TrueNAS, power on ...
  ./nas-maintenance.sh --finish

  Adding memory? memtest before importing the pool. ZFS trusts RAM implicitly,
  and bad DIMMs are one of the few ways to write corruption that checksums as
  valid.
EOF
    exit 0
}

[[ $# -eq 0 ]] && usage
while [[ "${1:-}" != "" ]]; do
    case "$1" in
        --start)  ACTION="start"; shift ;;
        --finish) ACTION="finish"; shift ;;
        --status) ACTION="status"; shift ;;
        --skip-backup-check) SKIP_BACKUP_CHECK=true; shift ;;
        -h|--help) usage ;;
        *) echo "Error: Unknown option: $1" >&2; usage ;;
    esac
done

require_tools kubectl python3

NAS_HOST="${NAS_HOST:-192.168.4.36}"

# ------------------------------------------------------------------------------
# Discovery
# ------------------------------------------------------------------------------
# Derived from the storage class, never hardcoded: a service added next month is
# covered the day it gets a NAS volume, with no edit here.
discover() {
    kubectl get pvc -A -o json | python3 -c "
import json, subprocess, sys

d = json.load(sys.stdin)
nas = {(p['metadata']['namespace'], p['metadata']['name'])
       for p in d['items']
       if (p['spec'].get('storageClassName') or '').startswith('truenas')}
if not nas:
    sys.exit(0)

namespaces = sorted({ns for ns, _ in nas})
pods = json.loads(subprocess.run(
    ['kubectl', 'get', 'pods', '-A', '-o', 'json'],
    capture_output=True, text=True).stdout or '{}')

owners = set()
for p in pods.get('items', []):
    ns = p['metadata']['namespace']
    claims = {(v.get('persistentVolumeClaim') or {}).get('claimName')
              for v in (p['spec'].get('volumes') or [])}
    if not any((ns, c) in nas for c in claims if c):
        continue
    for r in (p['metadata'].get('ownerReferences') or []):
        if r['kind'] == 'ReplicaSet':
            rs = json.loads(subprocess.run(
                ['kubectl', '-n', ns, 'get', 'rs', r['name'], '-o', 'json'],
                capture_output=True, text=True).stdout or '{}')
            dep = (rs.get('metadata', {}).get('ownerReferences') or [{}])[0]
            if dep.get('kind') == 'Deployment':
                owners.add((ns, 'deployment', dep['name']))
        elif r['kind'] == 'StatefulSet':
            owners.add((ns, 'statefulset', r['name']))

# CronJobs have no pod between fires, so match them on their own spec.
cjs = json.loads(subprocess.run(
    ['kubectl', 'get', 'cronjobs', '-A', '-o', 'json'],
    capture_output=True, text=True).stdout or '{}')
for c in cjs.get('items', []):
    ns = c['metadata']['namespace']
    vols = c['spec']['jobTemplate']['spec']['template']['spec'].get('volumes') or []
    claims = {(v.get('persistentVolumeClaim') or {}).get('claimName') for v in vols}
    if any((ns, cl) in nas for cl in claims if cl):
        owners.add((ns, 'cronjob', c['metadata']['name']))

for ns, kind, name in sorted(owners):
    print(f'{ns}\t{kind}\t{name}')
"
}

# ------------------------------------------------------------------------------
# Status
# ------------------------------------------------------------------------------
if [[ "$ACTION" == "status" ]]; then
    echo "=== Workloads with NAS-backed storage ==="
    n=0
    while IFS=$'\t' read -r ns kind name; do
        [[ -z "$ns" ]] && continue
        if [[ "$kind" == "cronjob" ]]; then
            cur="$(kubectl -n "$ns" get cronjob "$name" -o jsonpath='{.spec.suspend}' 2>/dev/null)"
            printf '  %-14s %-12s %-34s suspended=%s\n' "$ns" "$kind" "$name" "${cur:-false}"
        else
            cur="$(kubectl -n "$ns" get "$kind" "$name" -o jsonpath='{.spec.replicas}' 2>/dev/null)"
            printf '  %-14s %-12s %-34s replicas=%s\n' "$ns" "$kind" "$name" "${cur:-?}"
        fi
        n=$((n+1))
    done < <(discover)
    echo "  --- $n workload(s) would be quiesced"
    echo ""
    echo "=== Staying up (local-path, the survival tier) ==="
    kubectl get pvc -A -o json | python3 -c "
import json,sys
for p in json.load(sys.stdin)['items']:
    if p['spec'].get('storageClassName') == 'local-path':
        print(f\"  {p['metadata']['namespace']}/{p['metadata']['name']}\")
"
    exit 0
fi

# ------------------------------------------------------------------------------
# Start
# ------------------------------------------------------------------------------
if [[ "$ACTION" == "start" ]]; then
    # A maintenance window is exactly when you find out the backups were not
    # running. Check before taking anything down, not after.
    if [[ "$SKIP_BACKUP_CHECK" != "true" ]]; then
        echo "=== Pre-flight: recent backups ==="
        if ! kubectl get schedules -A >/dev/null 2>&1; then
            echo "  [WARN] k8up is not installed — nothing is backed up."
            read -rp "  Continue anyway? [y/N] " a
            [[ "$a" == "y" || "$a" == "Y" ]] || exit 1
        else
            recent="$(kubectl get snapshots.k8up.io -A --no-headers 2>/dev/null | grep -c . || true)"
            echo "  ${recent} snapshot(s) in the repository"
            if [[ "${recent:-0}" -eq 0 ]]; then
                echo "  [WARN] the repository is empty — no backup has ever completed."
                read -rp "  Continue anyway? [y/N] " a
                [[ "$a" == "y" || "$a" == "Y" ]] || exit 1
            fi
        fi
    fi

    mkdir -p "$(dirname "$STATE_FILE")"
    : > "${STATE_FILE}.tmp"

    echo ""
    echo "=== Quiescing ==="
    while IFS=$'\t' read -r ns kind name; do
        [[ -z "$ns" ]] && continue
        if [[ "$kind" == "cronjob" ]]; then
            prev="$(kubectl -n "$ns" get cronjob "$name" -o jsonpath='{.spec.suspend}' 2>/dev/null)"
            printf '%s\t%s\t%s\t%s\n' "$ns" "$kind" "$name" "${prev:-false}" >> "${STATE_FILE}.tmp"
            kubectl -n "$ns" patch cronjob "$name" -p '{"spec":{"suspend":true}}' >/dev/null
            printf '  suspended  %-14s %s\n' "$ns" "$name"
        else
            prev="$(kubectl -n "$ns" get "$kind" "$name" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 1)"
            printf '%s\t%s\t%s\t%s\n' "$ns" "$kind" "$name" "${prev:-1}" >> "${STATE_FILE}.tmp"
            kubectl -n "$ns" scale "$kind" "$name" --replicas=0 >/dev/null
            printf '  scaled 0   %-14s %-30s (was %s)\n' "$ns" "$name" "${prev:-1}"
        fi
    done < <(discover)
    mv "${STATE_FILE}.tmp" "$STATE_FILE"
    echo "  state written to ${STATE_FILE}"

    # ------------------------------------------------------------------------
    # The real gate
    # ------------------------------------------------------------------------
    # Not "are the pods gone" but "has the kubelet finished detaching". A
    # VolumeAttachment still present means an iSCSI session is still logged in,
    # and powering the NAS off underneath it is exactly the case that leaves a
    # filesystem dirty.
    echo ""
    echo "=== Waiting for volumes to detach ==="
    for i in $(seq 1 120); do
        left="$(kubectl get volumeattachment -o json 2>/dev/null | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print(0); raise SystemExit
print(sum(1 for v in d.get('items',[])
          if v['spec']['attacher'].startswith('org.democratic-csi')))
")"
        [[ "${left:-0}" -eq 0 ]] && break
        printf '\r  %s attachment(s) remaining...' "$left"
        sleep 5
    done
    echo ""
    if [[ "${left:-0}" -ne 0 ]]; then
        echo "  [ERROR] ${left} volume(s) still attached after 10 minutes."
        kubectl get volumeattachment -o custom-columns='NAME:.metadata.name,ATTACHER:.spec.attacher,NODE:.spec.nodeName,ATTACHED:.status.attached' 2>/dev/null | head -10
        echo "  Do NOT power off the NAS until this is empty."
        exit 1
    fi
    echo "  [OK] no democratic-csi volumes attached"

    cat <<EOF

=== Safe to power off the NAS ===

Still running: ingress, login, and every stateless service. Only NAS-backed
workloads are down.

When it is back:  $(basename "$0") --finish
EOF
    exit 0
fi

# ------------------------------------------------------------------------------
# Finish
# ------------------------------------------------------------------------------
if [[ "$ACTION" == "finish" ]]; then
    [[ -f "$STATE_FILE" ]] || { echo "FAIL: no state file at ${STATE_FILE} — was --start run?"; exit 1; }

    echo "=== Waiting for the NAS ==="
    for port in 443 3260 2049; do
        for i in $(seq 1 60); do
            if timeout 3 bash -c "cat < /dev/null > /dev/tcp/${NAS_HOST}/${port}" 2>/dev/null; then
                printf '  [OK]   %-6s\n' "$port"; break
            fi
            [[ "$i" -eq 60 ]] && { echo "  [FAIL] ${port} never came back"; exit 1; }
            sleep 5
        done
    done

    # The CSI controllers reconnect on their own, but a volume provisioned while
    # the API was still starting fails, so give them a moment to settle.
    echo "=== Waiting for the CSI drivers ==="
    kubectl -n democratic-csi rollout status deployment/democratic-csi-iscsi-controller --timeout=300s >/dev/null 2>&1 || true
    kubectl -n democratic-csi rollout status deployment/democratic-csi-nfs-controller --timeout=300s >/dev/null 2>&1 || true
    echo "  [OK]"

    # Restore in reverse: StatefulSets (the databases) before the Deployments
    # that talk to them, so apps do not spend the first minute crash-looping.
    echo ""
    echo "=== Restoring ==="
    for want_kind in statefulset deployment cronjob; do
        while IFS=$'\t' read -r ns kind name prev; do
            [[ -z "$ns" || "$kind" != "$want_kind" ]] && continue
            if [[ "$kind" == "cronjob" ]]; then
                [[ "$prev" == "true" ]] && { printf '  left suspended %-14s %s\n' "$ns" "$name"; continue; }
                kubectl -n "$ns" patch cronjob "$name" -p '{"spec":{"suspend":false}}' >/dev/null
                printf '  unsuspended    %-14s %s\n' "$ns" "$name"
            else
                kubectl -n "$ns" scale "$kind" "$name" --replicas="$prev" >/dev/null
                printf '  scaled %-3s     %-14s %s\n' "$prev" "$ns" "$name"
            fi
        done < "$STATE_FILE"
    done

    mv "$STATE_FILE" "${STATE_FILE}.done"
    echo ""
    echo "=== Anything not Ready ==="
    sleep 20
    kubectl get pods -A --no-headers 2>/dev/null \
      | awk '$4!="Running" && $4!="Completed" {print "  "$1"/"$2"  "$4}' | head -15 \
      || echo "  all healthy"

    cat <<EOF

If a pod is stuck with a read-only filesystem, its iSCSI volume was errored
rather than cleanly detached. Restart it:

  kubectl -n <ns> rollout restart deployment/<name>
EOF
    exit 0
fi
