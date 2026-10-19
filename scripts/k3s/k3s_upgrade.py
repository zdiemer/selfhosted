#!/usr/bin/env python3
"""Explicit, one-node K3s maintenance with off-node recovery archives.

Check is read-only. Prepare downloads/backups but never restarts K3s.
Execute requires direct SSH; privileged pods are preparation-only transport.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import time

VERSION = re.compile(r"^v(\d+)\.(\d+)\.(\d+)\+k3s(\d+)$")


def version(value):
    match = VERSION.fullmatch(value)
    if not match:
        raise ValueError(f"Invalid K3s version: {value!r}")
    return tuple(map(int, match.groups()))


def run(args, *, data=None, timeout=30):
    return subprocess.check_output(args, input=data, timeout=timeout)


def kube(*args):
    # Bound the subprocess without overriding kubectl connection defaults.
    return run(["kubectl", *args])


def server(node):
    labels = node["metadata"].get("labels", {})
    return any("node-role.kubernetes.io/" + role in labels
               for role in ("control-plane", "master"))


def validate_step(nodes, name, target):
    wanted = version(target)
    selected = next((n for n in nodes if n["metadata"]["name"] == name), None)
    if selected is None:
        raise ValueError(f"Unknown node: {name}")
    current = version(selected["status"]["nodeInfo"]["kubeletVersion"])
    if wanted < current:
        raise ValueError("Downgrades require the documented snapshot recovery procedure")
    if wanted[0] != current[0] or wanted[1] - current[1] > 1:
        raise ValueError("Cannot skip Kubernetes minor versions")
    servers = [n for n in nodes if server(n)]
    if not servers:
        raise ValueError("No control-plane nodes found")
    # Also constrain a server against its peers, not just its own version.
    oldest = min(version(n["status"]["nodeInfo"]["kubeletVersion"]) for n in servers)
    if wanted[0] != oldest[0] or wanted[1] > oldest[1] + int(server(selected)):
        raise ValueError("Upgrade all servers through the intermediate minor first")
    if not server(selected) and wanted[:3] > oldest[:3]:
        raise ValueError("Upgrade all servers before advancing this agent")
    return selected


def check_ready(nodes):
    for node in nodes:
        conditions = {c["type"]: c["status"] for c in node["status"]["conditions"]}
        if conditions.get("Ready") != "True" or any(
            conditions.get(c) == "True" for c in ("MemoryPressure", "DiskPressure", "PIDPressure")
        ):
            raise ValueError(f"Unhealthy node: {node['metadata']['name']}")


def host(node, script, transport="ssh", timeout=30):
    if transport == "pod":
        pods = json.loads(kube("-n", "infra", "get", "pods", "-o", "json"))["items"]
        candidates = [p for p in pods if p["metadata"]["name"].startswith("registry-node-config-")
                      and p["spec"].get("nodeName") == node
                      and p["status"].get("phase") == "Running"]
        if len(candidates) != 1:
            raise ValueError(f"Expected one existing privileged access pod on {node}")
        args = ["kubectl", "-n", "infra", "exec", "-i", candidates[0]["metadata"]["name"],
                "--", "nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "bash", "-s"]
    else:
        args = ["tailscale", "ssh", "root@" + node, "bash -s"]
    return run(args, data=("set -euo pipefail\n" + script).encode(), timeout=timeout)


def cluster_health(nodes, transport):
    check_ready(nodes)
    for node in nodes:
        if not server(node):
            continue
        name = node["metadata"]["name"]
        raw = host(name, "curl -fsS --max-time 10 http://127.0.0.1:2381/health", transport)
        if json.loads(raw).get("health") not in (True, "true"):
            raise ValueError(f"etcd unhealthy on {name}")
        host(name, "k3s kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml "
             "get --raw=/readyz --request-timeout=15s >/dev/null", transport)


def workload_health():
    workloads = json.loads(kube("get", "deployments,statefulsets", "-A", "-o", "json"))["items"]
    for item in workloads:
        desired = item["spec"].get("replicas", 1)
        status = item.get("status", {})
        if status.get("readyReplicas", 0) < desired or status.get("observedGeneration", 0) < item["metadata"].get("generation", 0):
            raise ValueError(f"Workload not settled: {item['metadata']['namespace']}/{item['metadata']['name']}")


def prepare(node, target, recovery, transport):
    name = node["metadata"]["name"]
    unit = "k3s" if server(node) else "k3s-agent"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stage = f"/var/lib/rancher/k3s/upgrade/{target}-{stamp}"
    original = shlex.quote(node["status"]["nodeInfo"]["kubeletVersion"])
    # No installer: service arguments, environment files and drop-ins stay intact.
    script = f"""
umask 077
stage={shlex.quote(stage)}
version={shlex.quote(target)}
unit={unit}
test "$(id -u)" = 0
systemctl is-active --quiet "$unit"
test -f /usr/local/bin/k3s && test ! -L /usr/local/bin/k3s
test "$(/usr/local/bin/k3s --version | head -1 | awk '{{print $3}}')" = {original}
# This workflow is deliberately limited to the standard systemd installation.
systemctl show "$unit" -p ExecStart --value | grep -q '/usr/local/bin/k3s'
if systemctl show "$unit" -p ExecStart --value | grep -q -- '--data-dir'; then
 echo 'Custom data-dir requires a separate recovery procedure' >&2; exit 1
fi
if grep -qsE '^[[:space:]]*data-dir:' /etc/rancher/k3s/config.yaml /etc/rancher/k3s/config.yaml.d/*.yaml || grep -qs 'K3S_DATA_DIR=' /etc/systemd/system/"$unit".service.env; then
 echo 'Configured data-dir requires a separate recovery procedure' >&2; exit 1
fi
mkdir -p "$stage"
case "$(uname -m)" in
 x86_64) asset=k3s; arch=amd64 ;;
 aarch64) asset=k3s-arm64; arch=arm64 ;;
 *) echo 'Unsupported architecture' >&2; exit 1 ;;
esac
url="https://github.com/k3s-io/k3s/releases/download/$version"
curl -fsSL --retry 3 --connect-timeout 15 --max-time 300 "$url/$asset" -o "$stage/k3s.new"
curl -fsSL --retry 3 --connect-timeout 15 --max-time 60 "$url/sha256sum-$arch.txt" -o "$stage/checksums"
expected=$(awk -v asset="$asset" '$2 == asset || $2 == "*" asset {{print $1}}' "$stage/checksums")
[[ "$expected" =~ ^[0-9a-f]{{64}}$ ]]
printf '%s  %s\\n' "$expected" "$stage/k3s.new" | sha256sum -c - >/dev/null
chmod 755 "$stage/k3s.new"
test "$("$stage/k3s.new" --version | head -1 | awk '{{print $3}}')" = "$version"
cp -p /usr/local/bin/k3s "$stage/k3s.previous"
paths=(etc/rancher/k3s etc/systemd/system usr/local/bin/k3s)
if [ "$unit" = k3s ]; then
 test -s /var/lib/rancher/k3s/server/token
 snapshot="upgrade-$version-{stamp}"
 k3s etcd-snapshot save --name "$snapshot" >&2
 shopt -s nullglob
 snapshots=(/var/lib/rancher/k3s/server/db/snapshots/"$snapshot"-*)
 test "${{#snapshots[@]}}" = 1
 paths+=("${{snapshots[0]#/}}" var/lib/rancher/k3s/server/token var/lib/rancher/k3s/server/cred)
fi
tar -C / -czf "$stage/recovery.tgz" "${{paths[@]}}"
sha256sum "$stage/recovery.tgz" "$stage/k3s.new"
"""
    output = host(name, script, transport, timeout=600).decode().splitlines()
    archive_sha = output[-2].split()[0]
    binary_sha = output[-1].split()[0]
    recovery.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(recovery, 0o700)
    archive = recovery / f"{name}-{stamp}.tgz"
    # Bytes are never printed; this archive contains credentials.
    with archive.open("xb") as out:
        os.chmod(archive, 0o600)
        out.write(host(name, f"cat {shlex.quote(stage + '/recovery.tgz')}", transport, timeout=180))
    if hashlib.sha256(archive.read_bytes()).hexdigest() != archive_sha:
        raise ValueError("Off-node recovery archive checksum mismatch")
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        if "usr/local/bin/k3s" not in names:
            raise ValueError("Recovery archive lacks old binary")
        if server(node) and ("var/lib/rancher/k3s/server/token" not in names or not any(
                n.startswith("var/lib/rancher/k3s/server/db/snapshots/upgrade-") for n in names)):
            raise ValueError("Recovery archive lacks snapshot or matching token")
    manifest = dict(node=name, target=target, stage=stage, archive=str(archive.resolve()),
                    archive_sha256=archive_sha, binary_sha256=binary_sha,
                    prepared_at=dt.datetime.now(dt.timezone.utc).isoformat(), unit=unit,
                    original_version=node["status"]["nodeInfo"]["kubeletVersion"])
    path = recovery / f"{name}.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    os.chmod(path, 0o600)
    print(f"Prepared {name}; verified off-node recovery archive: {archive}")


def load_prepared(path, node, target):
    m = json.loads(path.read_text())
    if m["node"] != node["metadata"]["name"] or m["target"] != target:
        raise ValueError("Prepared manifest does not match this node and target")
    if m["original_version"] != node["status"]["nodeInfo"]["kubeletVersion"]:
        raise ValueError("Node version changed since preparation")
    if dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(m["prepared_at"]) > dt.timedelta(hours=24):
        raise ValueError("Preparation is older than 24 hours; take fresh recovery backups")
    if hashlib.sha256(Path(m["archive"]).read_bytes()).hexdigest() != m["archive_sha256"]:
        raise ValueError("Recovery archive checksum mismatch")
    return m


def execute(node, target, manifest):
    name = node["metadata"]["name"]
    stage = shlex.quote(manifest["stage"])
    unit = "k3s" if server(node) else "k3s-agent"
    old_invocation = host(name, f"systemctl show {unit} -p InvocationID --value").strip()
    host(name, f"printf '%s  %s\\n' {shlex.quote(manifest['binary_sha256'])} {stage}/k3s.new | sha256sum -c - >/dev/null")
    # Atomic create prevents two operators from taking down different members.
    # Keep the lock on failure; an operator clears it only after recovery.
    kube("-n", "kube-system", "create", "configmap", "k3s-upgrade-lock",
         "--from-literal=node=" + name, "--from-literal=target=" + target)
    latest_nodes = json.loads(kube("get", "nodes", "-o", "json"))["items"]
    latest = validate_step(latest_nodes, name, target)
    if latest["status"]["nodeInfo"]["kubeletVersion"] != node["status"]["nodeInfo"]["kubeletVersion"]:
        raise ValueError("Version changed while acquiring lock; review and clear k3s-upgrade-lock")
    check_ready(latest_nodes)
    was_cordoned = latest.get("spec", {}).get("unschedulable", False)
    kube("cordon", name)
    # A host-owned unit survives the SSH connection and an in-cluster caller's loss.
    job = "k3s-upgrade-" + str(int(time.time()))
    started = dt.datetime.now(dt.timezone.utc)
    body = (f"set -euo pipefail; install -m 755 {stage}/k3s.new /usr/local/bin/k3s.upgrade; "
            f"mv /usr/local/bin/k3s.upgrade /usr/local/bin/k3s; systemctl restart {unit}; "
            f"systemctl is-active --quiet {unit}")
    try:
        host(name, "systemd-run --collect --unit=" + job + " /bin/bash -c " + shlex.quote(body))
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                fresh = json.loads(kube("get", "node", name, "-o", "json"))
                lease = json.loads(kube("-n", "kube-node-lease", "get", "lease", name, "-o", "json"))
                renewed = dt.datetime.fromisoformat(lease["spec"]["renewTime"].replace("Z", "+00:00"))
                invocation = host(name, f"systemctl is-active --quiet {unit}; systemctl show {unit} -p InvocationID --value").strip()
                check_ready([fresh])
                if (fresh["status"]["nodeInfo"]["kubeletVersion"] == target
                        and invocation and invocation != old_invocation and renewed > started):
                    break
            except (subprocess.SubprocessError, ValueError, KeyError):
                pass
            time.sleep(5)
        else:
            raise ValueError("Timed out waiting for target version, new service invocation and fresh Ready lease")
        # Two successful all-server checks, separated by a settling interval.
        for attempt in range(2):
            if attempt:
                time.sleep(15)
            cluster_health(json.loads(kube("get", "nodes", "-o", "json"))["items"], "ssh")
        workload_health()
        if not was_cordoned:
            kube("uncordon", name)
    except BaseException:
        print(f"STOP: {name} remains cordoned. Inspect journalctl -u {job} and {unit}; "
              "do not downgrade a server without the snapshot recovery procedure.", file=sys.stderr)
        raise
    kube("-n", "kube-system", "delete", "configmap", "k3s-upgrade-lock")
    print(f"Verified {name} at {target}; original cordon state restored. Review workloads before the next node.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", required=True, help="Exact pinned release, e.g. v1.35.9+k3s1")
    p.add_argument("--node", required=True, help="One node per invocation; servers first")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true", help="Download and back up, without restarting")
    mode.add_argument("--execute", action="store_true", help="Upgrade one prepared node via SSH")
    p.add_argument("--transport", choices=("ssh", "pod"), default="ssh", help="Pod access is preparation/check only")
    p.add_argument("--recovery-dir", type=Path, help="Private off-node persistent directory (never Git)")
    args = p.parse_args()
    version(args.version)
    if args.execute and args.transport != "ssh":
        p.error("Execution requires direct SSH recovery access; pod access is preparation-only")
    if (args.prepare or args.execute) and args.recovery_dir is None:
        p.error("--recovery-dir is required for preparation and execution")
    if args.recovery_dir:
        resolved = args.recovery_dir.resolve()
        if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
            p.error("Recovery archives contain secrets; choose a directory outside Git")
    nodes = json.loads(kube("get", "nodes", "-o", "json"))["items"]
    node = validate_step(nodes, args.node, args.version)
    cluster_health(nodes, args.transport)
    if args.execute and node["status"]["nodeInfo"]["kubeletVersion"] == args.version:
        print(f"{args.node} already runs {args.version}; no restart needed")
        return
    host(args.node, "test \"$(id -u)\" = 0; command -v systemd-run >/dev/null", args.transport)
    if args.prepare:
        prepare(node, args.version, args.recovery_dir, args.transport)
    elif args.execute:
        workload_health()
        m = load_prepared(args.recovery_dir / f"{args.node}.json", node, args.version)
        execute(node, args.version, m)
    else:
        print(f"Checks passed for {args.node} -> {args.version}; no changes made")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
