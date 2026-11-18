#!/usr/bin/env python3
"""Copy ntfy's write-only `alerts` publisher password into this namespace.

The token-expiry CronJob needs the publisher credential, and a secretKeyRef
cannot cross namespaces. The alternative — adding the password to
op://homelab/dev-claude-workspace so ./upgrade.sh resolves it like every other
value here — would put one credential in two vault items, and the copy nobody
remembers is the one that breaks on the next rotation. infra/ntfy already seeds
the authoritative row into the cluster from its own item on every ntfy start, so
this copies that row instead. Same arrangement, and the same reasoning, as
talaria/scripts/k8s/provision-ntfy-secret.py.

Run once, and again after rotating the publisher password in the vault (the
rotation reaches ntfy's user.db on its next pod start, not this Secret). The
password is never decoded, printed, or written to disk.
"""

import json
import subprocess

SOURCE_NAMESPACE = "infra"
SOURCE_SECRET = "ntfy-seed"
SOURCE_KEY = "pw-alerts"
TARGET_NAMESPACE = "claude"
TARGET_SECRET = "claude-workspace-ntfy"

source = json.loads(subprocess.check_output(
    ["kubectl", "-n", SOURCE_NAMESPACE, "get", "secret", SOURCE_SECRET, "-o", "json"]))
password = source["data"].get(SOURCE_KEY)
if not password:
    raise SystemExit(
        f"{SOURCE_NAMESPACE}/{SOURCE_SECRET} has no {SOURCE_KEY}: the ntfy "
        "release has no `alerts` user. See infra/ntfy/values.local.yaml.example.")

secret = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": TARGET_SECRET, "namespace": TARGET_NAMESPACE},
    "type": "Opaque",
    "data": {"NTFY_PASSWORD": password},
}
subprocess.run(
    ["kubectl", "apply", "--server-side", f"--field-manager={TARGET_SECRET}", "-f", "-"],
    input=json.dumps(secret).encode(), check=True)
