# democratic-csi — TrueNAS-backed storage that isn't welded to a node

Two StorageClasses backed by the TrueNAS at `192.168.4.36`, replacing k3s
`local-path` for everything that isn't deliberately node-local.

| Class | Backend | Modes | Expansion | Snapshots | For |
|---|---|---|---|---|---|
| `truenas-iscsi` | ZFS zvol + ext4 | RWO | yes | yes | Databases, SQLite apps, Minecraft world |
| `truenas-nfs` | ZFS dataset | **RWX** | yes | yes | Media, backups, workspace home, restic repo |

## Why

Every `local-path` PV carries a `nodeAffinity` for whichever node first consumed
it. That means a pod can never be scheduled anywhere else, rolling updates
deadlock on `FailedAttachVolume`, and a dead node is a dead service. It also
means PVCs cannot be resized (`games/gamedex/README.md`), there is no RWX at all
(which is why self-hosted Loki was rejected in `infra/alloy/README.md` and why
RomM went to SMB), and there are no snapshots.

The NAS is not a new dependency — `romm-library`, `romm-saves` and
`smitele-bot-matchdata` have been SMB-mounted from this same box for a while.
This makes it first-class and adds ZFS redundancy, snapshots and expansion to
volumes that had none of the three.

## Deployed twice

democratic-csi runs one deployment per backend protocol, so this one chart
directory produces two releases:

```
democratic-csi-iscsi   values.yaml + values-iscsi.yaml   -> truenas-iscsi
democratic-csi-nfs     values.yaml + values-nfs.yaml     -> truenas-nfs
```

`upgrade.sh` installs both. Note that connection details are repeated in each
overlay rather than shared from `values.yaml` — helm does not template values
files, so a shared `truenas.host` key could not be read by the subchart. It would
look like configuration and do nothing.

A bare `helm lint infra/democratic-csi` **fails**, and that's correct: without an
overlay there is no `driver.config.driver`, and the chart has no valid
single-values form. Always lint the way it's installed:

```bash
helm lint infra/democratic-csi -f infra/democratic-csi/values.yaml \
                               -f infra/democratic-csi/values-iscsi.yaml
```

## Prerequisites (one-time, manual)

`upgrade.sh` pre-flights all of these and refuses to install until they pass,
because every one of them fails *silently* — helm reports success and the damage
shows up later as a PVC stuck `Pending`.

### 1. `infra/snapshot-controller` — install FIRST

A `VolumeSnapshotClass` is an instance of a CRD that chart owns. Install this one
first and the API server discards our snapshot classes without complaint.

```bash
./infra/snapshot-controller/upgrade.sh
```

### 2. Enable iSCSI and NFS on the TrueNAS

**Both ship disabled**, and both were refused when this migration started — only
SMB (445) and the UI (443) were listening.

*System Settings → Services* → toggle on **iSCSI** and **NFS**, and tick
*Start Automatically* for each.

### 3. Create the parent datasets

democratic-csi creates one **child** dataset per volume. It never creates
parents. Missing parents are the single most common cause of a PVC that never
binds.

```
tank/k8s
├── iscsi/v      zvols, one per RWO volume
├── iscsi/s      detached snapshots
├── nfs/v        datasets, one per RWX volume
├── nfs/s        detached snapshots
└── restic       k8up repository (see infra/k8up)
```

Substitute your real pool name for `tank` and update `datasetParentName` /
`detachedSnapshotsDatasetParentName` in both overlay files to match.

### 4. Create an API key

*Credentials → Users →* an account with dataset and sharing privileges *→ API
Keys → Add*. Then:

```bash
cp values.local.yaml.example values.local.yaml   # add the key
./scripts/secrets.sh import infra/democratic-csi/values.local.yaml
```

**Do not reuse the SMB account from `games/romm`.** That is a read-only share
user with no API rights. This key can create and destroy datasets, which makes it
the most powerful credential in this repo.

### 5. `open-iscsi` + `nfs-common` on every node

iSCSI volumes cannot mount without the former, NFS volumes without the latter. A
node missing either looks healthy until a stateful pod lands on it.

```bash
./scripts/k3s/iscsi-prereq.sh           # audit, changes nothing
./scripts/k3s/iscsi-prereq.sh --apply   # install + configure
```

New nodes get this from bootstrap (`~/Code/k3s-cluster`, Step 7). Note that an
earlier revision of `apply-updates.sh` actively *purged* `open-iscsi` on the
grounds Longhorn wasn't in use — it had already fired on three nodes. That block
is gone; don't reinstate it.

## Verification gate — run before migrating any real data

Both classes can install cleanly and still be unable to provision. Prove the
whole round trip on a throwaway volume first:

```bash
for SC in truenas-iscsi truenas-nfs; do
  kubectl apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: smoke-$SC, namespace: default }
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: $SC
  resources: { requests: { storage: 1Gi } }
EOF
done

kubectl get pvc -n default -w    # both must reach Bound
```

Then, for each class: write a file into it from a pod, take a `VolumeSnapshot`,
restore that snapshot into a second PVC, diff the contents, and expand the
original from 1Gi to 2Gi. **All five steps must work on both classes** before
anything real moves. Expansion is the one most likely to be quietly broken, and
it's the one `local-path` never had.

Clean up the smoke PVCs afterwards — `reclaimPolicy: Delete` means the zvols and
datasets go with them.

## Things worth knowing

**`volumeBindingMode: Immediate`, not `WaitForFirstConsumer`.** WFFC exists to
place a volume in the same zone or node as its first consumer, which is
meaningless when the volume lives on the NAS and every node reaches it equally.
Immediate also surfaces provisioning failures at PVC creation rather than at
first pod schedule, which is far easier to debug.

**iSCSI and NFS fail very differently.** NFS with `hard` mounts blocks I/O while
the NAS is away and resumes cleanly — pods hang, nothing corrupts. iSCSI returns
errors after `replacement_timeout` (300s, set by `iscsi-prereq.sh`), ext4
remounts **read-only**, and affected pods need a restart even after the NAS
returns. This asymmetry is why planned NAS maintenance is quiesced with
`scripts/k3s/nas-maintenance.sh` rather than ridden out.

**Never set `soft` on the NFS mount options.** It trades corruption risk for
convenience, and the restic repository lives on that class.

**`iscsid` is OOM-protected.** It was oom-killed on `zachd-ubuntu` on 2026-08-06.
A dead `iscsid` takes every iSCSI volume on that node read-only, so it now gets
`OOMScoreAdjust=-1000` — the same treatment as kubelet and containerd.

**Thin provisioning on iSCSI (`zvolEnableReservation: false`).** Declared PVC
sizes in this repo have always been nominal because local-path enforced no quota
at all. Reserving full capacity up front would abruptly make ~239GiB of
mostly-empty claims real. NFS *does* enable quotas, since one runaway volume
there could otherwise eat the pool the backups live on.

**What stays on `local-path` deliberately.** `auth/authelia-data` and the Traefik
`acme.json` — if the NAS is down and Authelia can't start, you can't log in to
anything to fix the NAS. Breaking that circular dependency costs 1.1Gi.
`infra/buildkit-cache` too: disposable, write-churny, and paying network latency
for a rebuildable cache is a straight loss.

## Verify

```bash
kubectl -n democratic-csi get pods
kubectl get storageclass
kubectl get volumesnapshotclass
```

The acceptance test for the whole migration is a drain — every migrated pod
should reschedule onto another node and come back Ready:

```bash
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
kubectl uncordon <node>
```
