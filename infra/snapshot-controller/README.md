# snapshot-controller — CSI snapshots for a cluster that has none

k3s bundles neither the `VolumeSnapshot` CRDs nor the controller that acts on
them. On a stock cluster:

```console
$ kubectl get volumesnapshotclass
error: the server doesn't have a resource type "volumesnapshotclass"
```

That is the state this cluster was in. Nothing here is optional — it's the
prerequisite that makes `infra/democratic-csi`'s snapshot classes mean anything.

Thin wrapper around [piraeus-charts/snapshot-controller][chart] (5.2.0,
external-snapshotter v8.6.0). All real configuration lives under the
`snapshot-controller:` key in `values.yaml`.

[chart]: https://artifacthub.io/packages/helm/piraeus-charts/snapshot-controller

## Install order

**Before `infra/democratic-csi`.** A `VolumeSnapshotClass` is an instance of a
CRD this chart owns. Install democratic-csi first and its snapshot classes are
unrecognized types — helm accepts them, the API server discards them, and every
snapshot you take afterwards silently does nothing.

```bash
./infra/snapshot-controller/upgrade.sh   # this, first
./infra/democratic-csi/upgrade.sh
```

## 🔴 `helm uninstall` on this chart destroys every snapshot you have

The upstream chart renders the CRDs into `templates/` with no
`helm.sh/resource-policy`, so helm treats them as release-owned and deletes them
on uninstall. Deleting a CRD cascade-deletes every object of that type. That
means:

1. Every `VolumeSnapshot` in the cluster goes.
2. Every `VolumeSnapshotContent` goes with it.
3. Each content object with `deletionPolicy: Delete` tells the CSI driver to
   remove the **underlying ZFS snapshot on the NAS** on its way out.

So a command that reads like a reversible "remove the controller" wipes the
entire snapshot layer, cluster-side and NAS-side, with no prompt.

`upgrade.sh` annotates the three CRDs `helm.sh/resource-policy=keep` after every
install, which makes helm leave them behind. It also prints the cluster-wide
snapshot count before and after and fails if it dropped.

If you genuinely need the CRDs gone, delete them deliberately and separately —
after confirming what that takes with them.

## Choices worth knowing

**One replica.** The controller is leader-elected, so a second replica buys
availability, not throughput. An outage delays snapshots; it doesn't lose data.

**No webhook.** `webhook.enabled: false`. It only validates snapshot object
shape, and costs a second deployment plus a certificate to keep alive. A webhook
that fails open is decoration; one that fails closed can block snapshot creation
outright. Not worth it here.

**`featureGates: null`, not `""`.** The upstream template renders its args map
unconditionally:

```gotemplate
{{- range $flag, $val := .Values.controller.args }}
- --{{ $flag | kebabcase }}={{ $val }}
{{- end }}
```

An empty string still emits a bare `--feature-gates=`. Only `null` drops the key
from the merged map. Same trap as the bare Traefik filter flag in `0a18466`.

**No nodeSelector or affinity.** Pinning a cluster-wide controller to a node is
the exact failure mode the storage migration exists to remove.

## Verify

```bash
kubectl get crd | grep snapshot
kubectl -n kube-system get pods -l app.kubernetes.io/instance=snapshot-controller
kubectl get volumesnapshotclass          # must not error
```

The real test is an end-to-end round trip, which belongs to
[`infra/democratic-csi`](../democratic-csi/): provision a PVC, snapshot it,
restore the snapshot into a new PVC, and diff the contents.
