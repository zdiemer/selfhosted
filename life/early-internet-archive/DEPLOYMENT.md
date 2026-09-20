# Deploying the archive reader

The dynamically provisioned `truenas-nfs` PVC is the canonical copy of the
archive. This means the root of the **claim**, mounted at `/archive` in the
pods—not the root of the NAS filesystem—contains the catalog built by
`build_library.py` and the source/media directories referenced by it:

```text
library.sqlite3
nsider2/...
indienerds/media/...
photobucket/media/...
```

The chart creates a retained 20 GiB `truenas-nfs` RWX claim by default. Set
`persistence.archive.existingClaim` and `persistence.archive.create=false` to
use a pre-provisioned claim instead. The Deployment mounts the claim read-only
and cannot alter preservation data. Files must be readable by uid/gid `65532`.

Build the disposable search projection, then copy the complete `data/` tree
into the claim through a short-lived, rootless writer pod:

```bash
python3 build_library.py
./sync-data.sh
```

For an incremental refresh after a source sweep, transfer only the rebuilt
catalog and changed preservation directories:

```bash
SYNC_PATHS="library.sqlite3 additional source_sweep giantbomb" ./sync-data.sh
```

`sync-data.sh` creates the default claim when it does not exist, streams the
local preservation tree into it, and verifies the copied catalog by SHA-256.
It does not delete unrecognized files already on the claim. To target an
existing claim, set `CLAIM`; to publish a data tree from another disk, set
`DATA_ROOT`.

Each pod's init container copies `library.sqlite3` to a private `emptyDir`.
SQLite therefore reads a local, immutable projection while recovered media is
streamed from NFS through `/asset/<id>`. Running `./upgrade.sh` always changes
the catalog revision annotation and rolls every pod, even if only the catalog
on the NAS changed.

The default host, `archive.zachd.duckdns.org`, follows the cluster's private
DuckDNS convention: it resolves to the tailnet ingress address and uses the
shared wildcard certificate. Traefik sends every route through an Authelia
forward-auth middleware. There is intentionally no Cloudflare/public host.

Build and push the image, then deploy and wait for both replicas:

```bash
./build.sh
./upgrade.sh
```

To render or validate without a cluster:

```bash
helm lint .
helm template early-internet-archive . -n life -f values.yaml
```

If a pod is stuck in `Init:Error`, inspect the copy step first:

```bash
kubectl -n life logs deploy/early-internet-archive -c copy-catalog
```

The usual causes are a missing `library.sqlite3`, insufficient read permission,
or a catalog larger than `catalogCopy.emptyDirSize`.
