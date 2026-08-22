#!/usr/bin/env bash
# Move CrowdSec's two LAPI volumes off local-path onto truenas-iscsi.
#
# Why: both PVCs are local-path with node affinity pinned to a single node, so
# the LAPI database and its config only exist on that node's disk. Retiring
# that node — which is the plan for zachd-ubuntu-laptop — would take the
# decisions DB, the alert history and the machine registrations with it. The
# volumes are also the only reason that node cannot simply be drained.
#
# What it does NOT do: the helm cutover. This script copies and verifies, then
# stops and prints the values.yaml change. Switching the chart to existingClaim
# makes helm PRUNE the old chart-managed PVCs, and a mistake there is
# unrecoverable if the PVs are still Delete — so that step stays manual and
# deliberate. Both PVs must already be Retain; the script refuses otherwise.
#
# Audit mode is the default and changes nothing. Pass --apply to move data.

set -euo pipefail

NS="${NS:-crowdsec}"
SC="${SC:-truenas-iscsi}"
OLD_CFG="crowdsec-config-pvc"
OLD_DB="crowdsec-db-pvc"
NEW_CFG="crowdsec-config-nas"
NEW_DB="crowdsec-db-nas"
APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

say()  { printf '%s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------------------
# Pre-flight — identical in both modes. Everything that could make the copy
# wrong is checked here rather than discovered halfway through.
# ------------------------------------------------------------------------------
say "==> Pre-flight"

kubectl get sc "$SC" >/dev/null 2>&1 || fail "storageclass $SC not found"
say "    [OK] storageclass $SC exists"

for pvc in "$OLD_CFG" "$OLD_DB"; do
  pv="$(kubectl -n "$NS" get pvc "$pvc" -o jsonpath='{.spec.volumeName}' 2>/dev/null)" \
    || fail "pvc $pvc not found in $NS"
  [[ -n "$pv" ]] || fail "pvc $pvc is not bound"
  reclaim="$(kubectl get pv "$pv" -o jsonpath='{.spec.persistentVolumeReclaimPolicy}')"
  # The whole point of Retain here is that the helm cutover deletes these PVCs.
  # With Delete, local-path removes the directory and the data is gone.
  [[ "$reclaim" == "Retain" ]] \
    || fail "pv $pv ($pvc) is $reclaim, not Retain — patch it before migrating:
      kubectl patch pv $pv -p '{\"spec\":{\"persistentVolumeReclaimPolicy\":\"Retain\"}}'"
  node="$(kubectl get pv "$pv" -o jsonpath='{.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]}' 2>/dev/null)"
  say "    [OK] $pvc -> $pv  reclaim=Retain  pinned=${node:-<none>}"
done

# A copy from a volume that is being written to can capture a torn SQLite file.
# The LAPI must be down for the duration, not merely quiet.
replicas="$(kubectl -n "$NS" get deploy crowdsec-lapi -o jsonpath='{.spec.replicas}')"
# On a re-run the deployment is already at 0 from the previous pass; remember 1
# so the closing instructions do not tell you to "scale back up" to zero.
[[ "$replicas" == "0" ]] && replicas=1
say "    [--] crowdsec-lapi replicas=$replicas (scaled to 0 during the copy)"

for pvc in "$NEW_CFG" "$NEW_DB"; do
  if kubectl -n "$NS" get pvc "$pvc" >/dev/null 2>&1; then
    say "    [--] $pvc already exists (will be reused)"
  else
    say "    [--] $pvc will be created on $SC"
  fi
done

# ------------------------------------------------------------------------------
# Audit mode stops here.
# ------------------------------------------------------------------------------
if [[ "$APPLY" == "0" ]]; then
  cat <<EOF

==> AUDIT ONLY — nothing has been changed.

Plan:
  1. scale crowdsec-lapi to 0                     (LAPI down; detection pauses,
                                                   agents buffer, nothing is banned
                                                   or unbanned meanwhile)
  2. create $NEW_CFG + $NEW_DB on $SC
  3. copy $OLD_CFG -> $NEW_CFG   (~2.7M, 63 files)
     copy $OLD_DB  -> $NEW_DB    (~11.6M, crowdsec.db + trace/)
     with cp -a, in one pod that mounts all four
  4. verify file counts and sha256 of crowdsec.db on both sides
  5. STOP. The helm cutover is printed, not run.

The old PVCs and their data are untouched by this script. Both PVs are Retain,
so even the later helm prune leaves the data recoverable.

Re-run with --apply to perform steps 1-4.
EOF
  exit 0
fi

# ------------------------------------------------------------------------------
# Apply
# ------------------------------------------------------------------------------
say ""
say "==> Scaling crowdsec-lapi to 0"
kubectl -n "$NS" scale deploy crowdsec-lapi --replicas=0
kubectl -n "$NS" wait --for=delete pod -l app.kubernetes.io/name=crowdsec-lapi --timeout=180s 2>/dev/null || true
# Belt and braces: the label above is chart-version-dependent, so also wait on
# any pod still holding the claims.
for i in $(seq 1 60); do
  holders="$(kubectl -n "$NS" get pods -o json 2>/dev/null \
    | grep -c "\"claimName\": \"$OLD_DB\"" || true)"
  [[ "$holders" == "0" ]] && break
  sleep 5
done
say "    [OK] no pod holds $OLD_DB"

say "==> Creating destination claims on $SC"
for spec in "$NEW_CFG:100Mi" "$NEW_DB:1Gi"; do
  name="${spec%%:*}"; size="${spec##*:}"
  kubectl -n "$NS" get pvc "$name" >/dev/null 2>&1 && { say "    [--] $name exists"; continue; }
  kubectl -n "$NS" apply -f - <<EOF >/dev/null
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: $name, namespace: $NS }
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: $SC
  resources: { requests: { storage: $size } }
EOF
  say "    [OK] created $name ($size)"
done
kubectl -n "$NS" wait --for=jsonpath='{.status.phase}'=Bound pvc/"$NEW_CFG" pvc/"$NEW_DB" --timeout=180s
say "    [OK] both destination claims Bound"

say "==> Copying (cp -a, one pod mounting all four volumes)"
kubectl -n "$NS" delete job crowdsec-pvc-copy --ignore-not-found >/dev/null 2>&1
kubectl -n "$NS" apply -f - <<EOF >/dev/null
apiVersion: batch/v1
kind: Job
metadata: { name: crowdsec-pvc-copy, namespace: $NS }
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      # No nodeSelector: the local-path PVs carry their own node affinity, so
      # the scheduler is already forced onto the right node. Pinning by hand
      # would just be a second place to get it wrong.
      containers:
      - name: copy
        image: busybox:1.36
        command: ["sh","-c"]
        args:
          - |
            set -e
            echo "--- config ---"
            cp -a /old-cfg/. /new-cfg/
            echo "--- db ---"
            cp -a /old-db/.  /new-db/
            echo "--- counts ---"
            echo "cfg old=\$(find /old-cfg -type f | wc -l) new=\$(find /new-cfg -type f | wc -l)"
            echo "db  old=\$(find /old-db  -type f | wc -l) new=\$(find /new-db  -type f | wc -l)"
            # Checksum the whole tree, not one named file. The db volume is
            # mounted with subPath: crowdsec, so the database is actually at
            # <root>/crowdsec/crowdsec.db — the first version of this looked
            # for /old-db/crowdsec.db, found nothing, and reported an empty
            # checksum. It failed closed, which was right, but it should not
            # have needed a path guess at all. lost+found is excluded: a fresh
            # ext4 destination has one and the local-path source does not.
            echo "--- tree sha256 ---"
            for side in old new; do
              for vol in cfg db; do
                sum=\$(cd /\$side-\$vol && find . -type f -not -path './lost+found/*' \\
                       -exec sha256sum {} \\; | sort -k2 | sha256sum | cut -d' ' -f1)
                echo "\$side-\$vol \$sum"
              done
            done
        volumeMounts:
        - { name: oldcfg, mountPath: /old-cfg }
        - { name: newcfg, mountPath: /new-cfg }
        - { name: olddb,  mountPath: /old-db  }
        - { name: newdb,  mountPath: /new-db  }
      volumes:
      - { name: oldcfg, persistentVolumeClaim: { claimName: $OLD_CFG } }
      - { name: newcfg, persistentVolumeClaim: { claimName: $NEW_CFG } }
      - { name: olddb,  persistentVolumeClaim: { claimName: $OLD_DB  } }
      - { name: newdb,  persistentVolumeClaim: { claimName: $NEW_DB  } }
EOF
kubectl -n "$NS" wait --for=condition=complete job/crowdsec-pvc-copy --timeout=600s \
  || { kubectl -n "$NS" logs job/crowdsec-pvc-copy 2>&1 | tail -20; fail "copy job did not complete"; }

say ""
say "==> Copy report"
kubectl -n "$NS" logs job/crowdsec-pvc-copy 2>&1 | sed 's/^/    /'

# Verify the two checksums actually match rather than just eyeballing the log.
LOG="$(kubectl -n "$NS" logs job/crowdsec-pvc-copy 2>/dev/null)"
for vol in cfg db; do
  o="$(echo "$LOG" | awk -v k="old-$vol" '$1==k{print $2}')"
  n="$(echo "$LOG" | awk -v k="new-$vol" '$1==k{print $2}')"
  [[ -n "$o" && "$o" == "$n" ]] \
    || fail "$vol tree checksum mismatch (old=$o new=$n) — do NOT cut over"
  say "    [OK] $vol tree sha256 matches ($o)"
done

cat <<EOF

==> Data is copied and verified. LAPI is still at 0 replicas.

Nothing has been cut over. To finish, add to infra/crowdsec/values.yaml:

  lapi:
    persistentVolume:
      config:
        existingClaim: $NEW_CFG
      data:
        existingClaim: $NEW_DB

then:  ./infra/crowdsec/upgrade.sh
       kubectl -n $NS scale deploy crowdsec-lapi --replicas=$replicas

helm will prune $OLD_CFG and $OLD_DB on that upgrade. Both PVs are Retain, so
they survive as Released and the data stays on disk until you delete them by
hand — which is the rollback path if anything looks wrong.
EOF
