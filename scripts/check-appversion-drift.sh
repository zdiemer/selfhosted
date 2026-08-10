#!/usr/bin/env bash
# Chart.yaml appVersion mirrors the chart's primary image tag (root README
# convention). Humans forget the Chart.yaml half when bumping after a build —
# finance/money once drifted v35 vs v79 — so CI enforces it.
#
# A tag "matches" when it equals appVersion or extends it with a variant
# suffix (2.11.0-fat, v0.24.0-rootless) or digest (tag@sha256:...).
#
# Charts are checked only when their primary tag can be located: the top-level
# `image.tag` in values.yaml, or an entry in the nested-path map below. Charts
# whose appVersion deliberately tracks something else go in SKIP.

set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

python3 - <<'EOF'
import sys, os, glob, subprocess, yaml

# appVersion legitimately differs from the image tag here:
#   egress-proxy — tag/binary mismatch is documented in the chart
SKIP = {"infra/egress-proxy"}

# Charts whose primary image lives somewhere other than top-level image.tag.
NESTED = {
    "discord/vocard": ("images", "bot", "tag"),   # bot leads
    "media/arr": ("apps", "sonarr", "tag"),       # sonarr leads (Chart.yaml comment)
}

# Submodules carry their own charts and their own CI; also absent in a
# non-recursive checkout.
subs = subprocess.run(
    ["git", "config", "--file", ".gitmodules", "--get-regexp", "path"],
    capture_output=True, text=True).stdout.split()
subs = {p for p in subs if "/" in p}

def dig(d, path):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d

failures = []
checked = 0
for chart_yaml in sorted(glob.glob("*/*/Chart.yaml")):
    d = os.path.dirname(chart_yaml)
    if d in SKIP or d in subs or any(d.startswith(s + "/") for s in subs):
        continue
    values_yaml = os.path.join(d, "values.yaml")
    if not os.path.exists(values_yaml):
        continue
    chart = yaml.safe_load(open(chart_yaml)) or {}
    values = yaml.safe_load(open(values_yaml)) or {}
    app = str(chart.get("appVersion", ""))
    tag = dig(values, NESTED.get(d, ("image", "tag")))
    if tag is None or app == "":
        continue  # no primary image tag to mirror
    tag = str(tag)
    checked += 1
    if tag == app or tag.startswith(app + "-") or tag.startswith(app + "@"):
        continue
    failures.append(f"  {d}: appVersion={app!r} but image tag={tag!r}")

if failures:
    print("appVersion drift (Chart.yaml must mirror the image tag):")
    print("\n".join(failures))
    sys.exit(1)
print(f"appVersion drift check: {checked} charts OK")
EOF
