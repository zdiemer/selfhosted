#!/usr/bin/env python3
"""Helm post-renderer: give the CSI node-registrar liveness probe a usable timeout.

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

Only DaemonSet documents are re-serialised; every other rendered document is
passed through byte for byte.
"""
import sys

import yaml

CONTAINER = "driver-registrar"
PROBE = {"timeoutSeconds": 15, "periodSeconds": 30}


def patch(doc):
    """Return True if this DaemonSet's registrar probe was changed."""
    spec = doc.get("spec", {}).get("template", {}).get("spec", {})
    changed = False
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

        if isinstance(doc, dict) and doc.get("kind") == "DaemonSet" and patch(doc):
            out.append(yaml.safe_dump(doc, default_flow_style=False, sort_keys=False))
            patched += 1
        else:
            out.append(chunk)

    if patched == 0:
        # A silent no-op here means the chart changed shape and the probe went
        # back to 1s without anyone noticing. Fail the deploy instead.
        sys.exit(
            "post-render.sh: no DaemonSet contained a '%s' livenessProbe to patch. "
            "The chart's node template changed -- re-check whether this "
            "post-renderer is still needed, or still correct." % CONTAINER
        )

    sys.stdout.write("\n---\n".join(out))


if __name__ == "__main__":
    main()
