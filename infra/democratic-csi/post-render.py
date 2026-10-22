#!/usr/bin/env python3
"""Helm post-renderer: host containment, registrar probes and block-publish fix.

WHY THIS EXISTS. The chart hardcodes the driver-registrar liveness probe in
templates/node.yaml with no timeoutSeconds and exposes no value to set one, so
it inherits the Kubernetes default of ONE SECOND for an exec probe. Three
missed seconds kills the container.

That is not theoretical. The restarts cluster on exactly the nodes that were
under pressure -- zachd-ubuntu, zachd-ubuntu-1, zachd-ubuntu-laptop-6 -- and
zachd-ubuntu-2 was sitting at 82% disk with a kubelet whose stats endpoint had
stopped answering. A 1s exec budget turns "this node is briefly busy" into
"restart the storage plugin", which is precisely backwards: the node plugin is
what you least want bouncing when a node is already struggling.

The `bin/liveness-probe: no such file or directory` events on the sibling
csi-driver container are downstream of this, not a separate bug. That path is
correct -- the image sets WorkingDir=/home/csi/app and even its entrypoint is
relative (`bin/democratic-csi`). containerd only reports it missing when it
resolves the relative path against a container whose process is already gone,
i.e. while the pod is restarting. Fix the restarts and those stop too.

WHAT IT SETS. timeout 1s -> 15s, period 10s -> 30s, matching the csi-driver
probe alongside it (timeout 15, period 60). Timeout had to move with period:
15s inside a 10s period would have probes treading on each other. Registration
state changes about once per pod lifetime, so probing it every ten seconds on a
one-second budget was both too often and too strict.

Remove this the day the chart accepts a value for it -- and send that upstream.

The iSCSI DaemonSet also gets an init container using the exact driver image.
It patches the block NodePublishVolume check into an emptyDir and mounts only
that module into the driver. The original check hardcodes devtmpfs source "dev";
Ubuntu reports "udev", causing valid existing mounts to fail reconciliation.
The replacement checks that both mounted paths are block devices with identical
rdev values. See patch-block-publish.js and test-block-publish.js. No host mount
or volume contents are modified by this patch.

Node plugins retain mount privilege but not host IPC. NFS drops the whole-host
root mount; iSCSI retains it for the iscsiadm chroot wrapper. Nonprivileged
sidecars and API controllers get capability dropping,
no-new-privileges and RuntimeDefault seccomp.
"""
import sys
from pathlib import Path

import yaml

CONTAINER = "driver-registrar"
PROBE = {"timeoutSeconds": 15, "periodSeconds": 30}


def patch(doc):
    """Apply the reviewed controller/node transformations."""
    spec = doc.get("spec", {}).get("template", {}).get("spec", {})
    changed = False
    if doc.get("kind") == "DaemonSet" and not any(
            c.get("name") == CONTAINER and c.get("livenessProbe")
            for c in spec.get("containers", [])):
        raise ValueError("node registrar probe disappeared; review upstream chart")
    for container in spec.get("containers") or []:
        if container.get("name") != CONTAINER:
            continue
        probe = container.get("livenessProbe")
        if not probe:
            continue
        for key, value in PROBE.items():
            if probe.get(key) != value:
                probe[key] = value
                changed = True
    if doc.get("metadata", {}).get("name", "").endswith("-iscsi-node"):
        driver = next(c for c in spec["containers"] if c["name"] == "csi-driver")
        patch_source = Path(__file__).with_name("patch-block-publish.js").read_text()
        volume = {"name": "block-publish-patch", "emptyDir": {}}
        init = {
            "name": "patch-block-publish",
            "image": driver["image"],
            "command": ["node", "-e", patch_source.replace(
                "if (require.main === module)", "if (true)"
            )],
            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                          "limits": {"memory": "128Mi"}},
            "volumeMounts": [{"name": volume["name"], "mountPath": "/driver-patch"}],
        }
        mount = {"name": volume["name"],
                 "mountPath": "/home/csi/app/src/driver/index.js",
                 "subPath": "index.js", "readOnly": True}
        for key, item in [("volumes", volume), ("initContainers", init)]:
            entries = spec.setdefault(key, [])
            entries[:] = [entry for entry in entries if entry["name"] != item["name"]]
            entries.append(item)
        mounts = driver.setdefault("volumeMounts", [])
        mounts[:] = [entry for entry in mounts if entry["name"] != mount["name"]]
        mounts.append(mount)
        changed = True
    # NFS does not need /host. iSCSI DOES: docker/iscsiadm runs the host
    # iscsiadm binary through chroot. Keep it until that architecture changes.
    if doc.get("kind") == "DaemonSet":
        spec["hostIPC"] = False
        if doc["metadata"]["name"].endswith("-nfs-node"):
            spec["volumes"] = [v for v in spec.get("volumes", []) if v["name"] != "host-dir"]
            for c in spec.get("containers", []):
                c["volumeMounts"] = [v for v in c.get("volumeMounts", []) if v["name"] != "host-dir"]
    for c in spec.get("containers", []) + spec.get("initContainers", []):
        context = c.setdefault("securityContext", {})
        if context.get("privileged"):
            continue
        context.update(allowPrivilegeEscalation=False,
                       capabilities={"drop": ["ALL"]},
                       seccompProfile={"type": "RuntimeDefault"})
        if c["name"] != "csi-driver":
            context["readOnlyRootFilesystem"] = True
        changed = True
    return changed


def main():
    raw = sys.stdin.read()
    out, patched = [], 0

    for chunk in raw.split("\n---\n"):
        if not chunk.strip():
            out.append(chunk)
            continue
        try:
            doc = yaml.safe_load(chunk)
        except yaml.YAMLError:
            # Not our business -- hand it back untouched rather than guess.
            out.append(chunk)
            continue

        if isinstance(doc, dict) and doc.get("kind") in ("DaemonSet", "Deployment") and patch(doc):
            out.append(yaml.safe_dump(doc, default_flow_style=False, sort_keys=False))
            if doc["kind"] == "DaemonSet":
                patched += 1
        else:
            out.append(chunk)

    if patched == 0:
        # A silent no-op here means the chart changed shape and the probe went
        # back to 1s without anyone noticing. Fail the deploy instead.
        sys.exit(
            "post-render.py: no DaemonSet contained a '%s' livenessProbe to patch. "
            "The chart's node template changed -- re-check whether this "
            "post-renderer is still needed, or still correct." % CONTAINER
        )

    sys.stdout.write("\n---\n".join(out))


if __name__ == "__main__":
    main()
