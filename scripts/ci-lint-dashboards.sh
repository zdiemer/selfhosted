#!/usr/bin/env bash
# Validate the Grafana dashboards and alert rules in infra/grafana-dashboards.
#
# These files are pushed to a SaaS API rather than rendered by helm, so
# scripts/ci-lint-charts.sh never sees them. Without this, a trailing comma or a
# missing uid is only discovered at push time — and the missing-uid case does
# not even fail then: Grafana cheerfully CREATES a second dashboard, so you end
# up with two copies of "Edge Traffic" and no idea which one is live.
#
# Deliberately offline. It needs no token and talks to nothing; it checks the
# things that are true of the files themselves.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="${ROOT}/infra/grafana-dashboards"

[[ -d "$DIR" ]] || { echo "no infra/grafana-dashboards — nothing to check"; exit 0; }

python3 - "$DIR" <<'PY'
import json, pathlib, re, sys

root = pathlib.Path(sys.argv[1])
problems = []

# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------
uids = {}
for f in sorted((root / 'dashboards').glob('*.json')):
    try:
        d = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        problems.append(f"{f.name}: invalid JSON — {e}")
        continue

    uid = d.get('uid')
    if not uid:
        problems.append(f"{f.name}: no top-level uid. Every push would create a NEW dashboard.")
    elif uid in uids:
        problems.append(f"{f.name}: uid '{uid}' is already used by {uids[uid]}")
    else:
        uids[uid] = f.name

    if not d.get('title'):
        problems.append(f"{f.name}: no title")

    panels = d.get('panels') or []
    if not panels:
        problems.append(f"{f.name}: no panels")

    def walk(ps):
        for p in ps:
            yield p
            if p.get('type') == 'row':
                yield from walk(p.get('panels') or [])

    for p in walk(panels):
        title = p.get('title', '<untitled>')
        if p.get('type') in ('row', 'text'):
            continue
        targets = p.get('targets') or []
        if not targets:
            problems.append(f"{f.name}: panel '{title}' has no targets")
            continue
        for t in targets:
            ds = (t.get('datasource') or p.get('datasource') or {})
            uidref = ds.get('uid', '')
            # The whole point of the sentinels: a real UID committed here is
            # per-stack, so it is wrong for any other stack and silently wrong
            # after a migration. upgrade.sh substitutes these at push time.
            if not uidref.startswith('__') or not uidref.endswith('__'):
                problems.append(
                    f"{f.name}: panel '{title}' references datasource uid '{uidref}' — "
                    f"use __PROM_UID__ / __LOKI_UID__ / __USAGE_UID__ instead")
            if not t.get('expr'):
                problems.append(f"{f.name}: panel '{title}' target {t.get('refId','?')} has no expr")

    print(f"  ok {f.name:<24} uid={uid or '-':<20} panels={len(panels)}")

# ---------------------------------------------------------------------------
# Alert rules
# ---------------------------------------------------------------------------
alerts = root / 'alerts' / 'rules.json'
if alerts.exists():
    try:
        rules = json.loads(alerts.read_text())
    except json.JSONDecodeError as e:
        problems.append(f"alerts/rules.json: invalid JSON — {e}")
        rules = []

    if not isinstance(rules, list):
        problems.append("alerts/rules.json: expected a top-level list of rules")
        rules = []

    seen = {}
    for r in rules:
        uid = r.get('uid')
        title = r.get('title', '<untitled>')
        if not uid:
            problems.append(f"alerts: rule '{title}' has no uid — re-running would duplicate it")
        elif uid in seen:
            problems.append(f"alerts: uid '{uid}' used by both '{seen[uid]}' and '{title}'")
        else:
            seen[uid] = title

        # Without this, the rule inherits Grafana's DEFAULT notification policy,
        # which on a Cloud stack means email. "Defined but not delivered" is a
        # property of every rule here and is worth enforcing rather than
        # remembering.
        recv = (r.get('notification_settings') or {}).get('receiver')
        if recv != 'unwired':
            problems.append(
                f"alerts: rule '{title}' routes to '{recv}', not 'unwired' — "
                f"it would fall through to the default policy and actually notify")

        if r.get('condition') not in {n.get('refId') for n in (r.get('data') or [])}:
            problems.append(f"alerts: rule '{title}' condition '{r.get('condition')}' matches no data node")

    print(f"  ok alerts/rules.json      {len(rules)} rules")

if problems:
    print()
    for p in problems:
        print(f"  FAIL {p}")
    sys.exit(1)
PY

echo "dashboards ok"
