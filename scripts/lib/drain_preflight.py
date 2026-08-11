"""Report what draining a node will actually cost, before you drain it.

Reads `kubectl get pods/pdb/pvc/nodes -A -o json` on stdin (one merged List),
takes node names as argv, and classifies every pod that a drain would evict.

WHY THIS EXISTS
`kubectl drain` tells you what it is deleting, not what that means. The
interesting questions before a maintenance window are which services go dark
and for how long, what cannot move at all, and what is going to sit waiting on
a volume — and none of those are visible in drain's output. Most of this
cluster is single-replica by necessity (single-writer RWO volumes), so "this
pod will be recreated elsewhere" is the normal case AND the outage.

Deliberately read-only. It prints; it never cordons, evicts, or patches.
"""

import json
import sys

# Storage classes whose volumes are network-attached but single-node ATTACH.
# A pod moving between nodes has to wait for a detach on the old node before
# the attach on the new one, which is the slow part of a drain and the part
# that occasionally wedges.
SLOW_DETACH_CLASSES = {"truenas-iscsi"}


def workload_of(pod):
    """Return (kind, name) of the controller, collapsing ReplicaSet -> Deployment."""
    refs = pod["metadata"].get("ownerReferences") or []
    if not refs:
        return ("Pod", pod["metadata"]["name"])
    ref = refs[0]
    if ref["kind"] == "ReplicaSet":
        return ("Deployment", ref["name"].rsplit("-", 1)[0])
    return (ref["kind"], ref["name"])


def matches(selector, labels):
    return selector and all(labels.get(k) == v for k, v in selector.items())


def elsewhere_matching(selector, node, nodes):
    """How many OTHER schedulable nodes satisfy this nodeSelector.

    Labels only. Taints and tolerations are not modelled, so this can still be
    optimistic for a pod that tolerates a taint nothing else does — but it is
    the difference between correctly saying "this can move" and flagging every
    label-based selector as pinned, which is what makes the report ignorable.
    """
    out = 0
    for n in nodes:
        if n["name"] == node or n["unschedulable"]:
            continue
        if all(n["labels"].get(k) == v for k, v in selector.items()):
            out += 1
    return out


def analyse(node, pods, pdbs, pvcs, replicas_by_workload, nodes):
    on_node = [
        p for p in pods
        if p["spec"].get("nodeName") == node
        and p["status"].get("phase") in ("Running", "Pending")
    ]

    gapping, moving, stuck, blocked, slow_volumes = [], [], [], [], []

    for p in on_node:
        ns = p["metadata"]["namespace"]
        name = p["metadata"]["name"]
        kind, wname = workload_of(p)
        spec = p["spec"]

        # DaemonSet pods are what --ignore-daemonsets skips: they are not
        # evicted and a copy stays on every other node, so they are not a gap.
        if kind == "DaemonSet":
            continue
        # A bare pod has no controller to recreate it. drain deletes it and it
        # is simply gone, which is worth saying out loud.
        if kind == "Pod":
            stuck.append((ns, name, "unmanaged pod - drain deletes it and nothing recreates it"))
            continue

        reasons = []
        sel = spec.get("nodeSelector") or {}
        if sel:
            if sel.get("kubernetes.io/hostname") == node:
                reasons.append(f"nodeSelector pins it to {node}")
            elif elsewhere_matching(sel, node, nodes) == 0:
                reasons.append(f"no other schedulable node matches nodeSelector {sel}")
        for v in spec.get("volumes") or []:
            if "hostPath" in v:
                reasons.append(f"hostPath {v['hostPath'].get('path')} exists only on this node")
        if reasons:
            stuck.append((ns, name, "; ".join(reasons)))
            continue

        replicas = replicas_by_workload.get((ns, kind, wname), 1)

        # Volumes that will have to detach here and attach elsewhere.
        for v in spec.get("volumes") or []:
            claim = (v.get("persistentVolumeClaim") or {}).get("claimName")
            if not claim:
                continue
            pvc = pvcs.get((ns, claim))
            if pvc and pvc.get("class") in SLOW_DETACH_CLASSES:
                slow_volumes.append((ns, name, claim, pvc["class"]))

        # A PDB with no disruptions allowed will refuse the eviction and drain
        # will retry until its timeout.
        for pdb in pdbs:
            if pdb["ns"] != ns:
                continue
            if matches(pdb["selector"], p["metadata"].get("labels") or {}):
                if pdb["allowed"] == 0:
                    blocked.append((ns, name, pdb["name"]))

        if replicas <= 1:
            gapping.append((ns, wname, kind))
        else:
            moving.append((ns, wname, kind, replicas))

    return {
        "gapping": sorted(set(gapping)),
        "moving": sorted(set(moving)),
        "stuck": sorted(stuck),
        "blocked": sorted(blocked),
        "slow_volumes": sorted(set(slow_volumes)),
        "total": len(on_node),
    }


def main():
    targets = sys.argv[1:]
    data = json.load(sys.stdin)

    pods, pdbs, pvcs, replicas_by_workload, nodes = [], [], {}, {}, []

    for item in data["items"]:
        kind = item.get("kind")
        ns = item["metadata"].get("namespace")
        if kind == "Pod":
            pods.append(item)
        elif kind == "PodDisruptionBudget":
            pdbs.append({
                "ns": ns,
                "name": item["metadata"]["name"],
                "selector": (item["spec"].get("selector") or {}).get("matchLabels") or {},
                "allowed": (item.get("status") or {}).get("disruptionsAllowed", 0),
            })
        elif kind == "PersistentVolumeClaim":
            pvcs[(ns, item["metadata"]["name"])] = {
                "class": item["spec"].get("storageClassName"),
                "modes": item["spec"].get("accessModes"),
            }
        elif kind in ("Deployment", "StatefulSet"):
            replicas_by_workload[(ns, kind, item["metadata"]["name"])] = item["spec"].get("replicas", 1)
        elif kind == "Node":
            nodes.append({
                "name": item["metadata"]["name"],
                "labels": item["metadata"].get("labels") or {},
                "unschedulable": bool(item["spec"].get("unschedulable")),
            })

    worst = 0
    for node in targets:
        r = analyse(node, pods, pdbs, pvcs, replicas_by_workload, nodes)
        print(f"\n=== {node} ===")
        print(f"  {r['total']} pod(s) here; DaemonSet pods are not counted (drain skips them).")

        if r["stuck"]:
            worst = max(worst, 3)
            print("\n  WILL NOT MOVE - these stay down until the node comes back:")
            for ns, name, why in r["stuck"]:
                print(f"    {ns}/{name}\n        {why}")

        if r["blocked"]:
            worst = max(worst, 3)
            print("\n  BLOCKED BY A PDB - drain will retry until it times out:")
            for ns, name, pdb in r["blocked"]:
                print(f"    {ns}/{name}  (budget {pdb} allows 0 disruptions right now)")

        if r["gapping"]:
            worst = max(worst, 2)
            print("\n  WILL GAP - single-replica, so the service is down while it moves:")
            for ns, wname, kind in r["gapping"]:
                print(f"    {ns}/{wname} ({kind})")

        if r["slow_volumes"]:
            print("\n  SLOW VOLUME HANDOFF - detach here must finish before attach there:")
            for ns, name, claim, cls in r["slow_volumes"]:
                print(f"    {ns}/{name} -> {claim} ({cls})")

        if r["moving"]:
            print("\n  Moves without a gap (multi-replica, budget permitting):")
            for ns, wname, kind, n in r["moving"]:
                print(f"    {ns}/{wname} ({kind}, {n} replicas)")

        if not (r["stuck"] or r["blocked"] or r["gapping"]):
            print("\n  Nothing here gaps. Safe to drain.")

    return worst


if __name__ == "__main__":
    sys.exit(main())
