#!/usr/bin/env bash
# Push the dashboards and alert rules in this directory to Grafana Cloud.
#
# THIS PROJECT DEPLOYS NOTHING TO THE CLUSTER. There is no chart and no helm
# release — the "deploy target" is a SaaS HTTP API. It keeps the repo's shape
# (an upgrade.sh per project, a gitignored values.local.yaml materialized from a
# tracked op:// template) so it behaves like every other directory here.
#
#   ./upgrade.sh              push dashboards + alert rules
#   ./upgrade.sh --verify     push, then run every panel query and report the
#                             ones returning nothing
#   ./upgrade.sh --dry-run    resolve datasource UIDs and validate the JSON,
#                             push nothing
#
# WHY THE TOKEN HERE IS NOT THE ONE IN infra/alloy. That one is an Access Policy
# token scoped logs:write + metrics:write — it can push data and literally
# nothing else. Creating a dashboard or running a query needs a token issued by
# the Grafana instance itself. See values.local.yaml.example.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LOCAL_VALUES="${HERE}/values.local.yaml"
DASHBOARD_DIR="${HERE}/dashboards"
ALERT_FILE="${HERE}/alerts/rules.json"
FOLDER_TITLE="${FOLDER_TITLE:-selfhosted}"

VERIFY=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --verify)  VERIFY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    *) echo "unknown flag: $arg"; exit 1 ;;
  esac
done

command -v curl    >/dev/null || { echo "curl required"; exit 1; }
command -v python3 >/dev/null || { echo "python3 required"; exit 1; }

# Same convenience as every other chart here: materialize from 1Password when
# the file is missing. values.local.yaml remains the contract.
if [[ ! -f "$LOCAL_VALUES" && -f "${HERE}/values.local.tpl.yaml" ]] && command -v op >/dev/null 2>&1; then
  echo "==> materializing values.local.yaml from 1Password"
  op inject -i "${HERE}/values.local.tpl.yaml" -o "$LOCAL_VALUES" \
    || { echo "FAIL: op inject failed. Signed in?  eval \$(op signin)"; exit 1; }
  chmod 600 "$LOCAL_VALUES"
fi

if [[ ! -f "$LOCAL_VALUES" ]]; then
  echo "missing ${LOCAL_VALUES}"
  echo "  cp values.local.yaml.example values.local.yaml   # then add the Grafana service-account token"
  exit 1
fi

read_cfg() {
  python3 -c "
import sys, yaml
d = yaml.safe_load(open('${LOCAL_VALUES}')) or {}
v = str((d.get('grafana') or {}).get('$1', ''))
if not v or 'REPLACE_ME' in v or v.startswith('glsa_0000'):
    sys.exit(1)
print(v.rstrip('/'))
"
}

GRAFANA_URL="$(read_cfg url)" || { echo "FAIL: grafana.url is unset or still a placeholder in ${LOCAL_VALUES}"; exit 1; }
GRAFANA_TOKEN="$(read_cfg token)" || { echo "FAIL: grafana.token is unset or still a placeholder in ${LOCAL_VALUES}"; exit 1; }

# Never put the token on a command line: argv is world-readable in /proc and
# lands in shell history. curl reads it from a file descriptor instead.
AUTH_HEADER_FILE="$(mktemp)"
cleanup() { rm -f "$AUTH_HEADER_FILE" "${TMP_FILES[@]:-}"; }
TMP_FILES=()
trap cleanup EXIT
printf 'Authorization: Bearer %s\n' "$GRAFANA_TOKEN" >"$AUTH_HEADER_FILE"

api() {
  # api <method> <path> [body-file]
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" -H @"$AUTH_HEADER_FILE" -H 'Content-Type: application/json' \
      --data-binary @"$body" "${GRAFANA_URL}${path}"
  else
    curl -sS -X "$method" -H @"$AUTH_HEADER_FILE" "${GRAFANA_URL}${path}"
  fi
}

api_code() {
  curl -sS -o /dev/null -w '%{http_code}' -X "${1}" -H @"$AUTH_HEADER_FILE" "${GRAFANA_URL}${2}"
}

# ---------------------------------------------------------------------------
# Permissions, checked before anything is attempted
# ---------------------------------------------------------------------------
# A service account created through the UI defaults to the **Viewer** role, and
# a Viewer token authenticates perfectly happily — /api/search returns [] rather
# than 401 — while every write and every datasource lookup 403s. Without this
# check the failure surfaces halfway through as an opaque "Access denied" on the
# third dashboard.
echo "==> checking token permissions"
DS_CODE="$(api_code GET /api/datasources)"
if [[ "$DS_CODE" == "403" ]]; then
  cat <<'EOF'
FAIL: the token authenticates but lacks permissions (403 on /api/datasources).

  The service account was almost certainly created with the default Viewer
  role. Admin is required: reading datasources is how this script resolves
  datasource UIDs instead of hardcoding them, and writing alert rules needs it
  outright.

  Fix WITHOUT issuing a new token:
    Grafana -> Administration -> Users and access -> Service accounts
      -> selfhosted-dashboards -> Role -> Admin

  The same glsa_ string then works.
EOF
  exit 1
fi
if [[ "$DS_CODE" != "200" ]]; then
  echo "FAIL: GET /api/datasources returned HTTP ${DS_CODE} at ${GRAFANA_URL}"
  echo "      Check grafana.url — it must be the Grafana instance"
  echo "      (https://<stack>.grafana.net), not grafana.com and not the"
  echo "      Prometheus/Loki push endpoints."
  exit 1
fi

# ---------------------------------------------------------------------------
# Resolve datasource UIDs
# ---------------------------------------------------------------------------
# Dashboards carry the sentinels __PROM_UID__, __LOKI_UID__ and __USAGE_UID__
# rather than real UIDs. Hardcoding `grafanacloud-<slug>-prom` is the classic
# way these files rot: the UID is per-stack, so a committed one is wrong for
# anybody else and silently wrong after a stack migration.
echo "==> resolving datasource UIDs"
DS_JSON="$(mktemp)"; TMP_FILES+=("$DS_JSON")
api GET /api/datasources >"$DS_JSON"

eval "$(python3 - "$DS_JSON" <<'PY'
import json, sys, shlex

ds = json.load(open(sys.argv[1]))

# MATCH BY UID, NOT BY TYPE.
#
# A Grafana Cloud stack ships THREE Loki datasources — the real log store, the
# alert state history, and usage insights — and picking "the first one of type
# loki" silently selected `grafanacloud-alert-state-history`. Every panel then
# queries a store that has never seen a cluster log: the dashboards push
# cleanly, render without error, and are uniformly empty.
#
# The uids are stable per-stack names assigned by Grafana Cloud, so matching
# them exactly is both safe and self-documenting. Anything unexpected is a hard
# failure rather than a guess, because the guess is invisible once it is wrong.
WANT = {
    'PROM':  ('grafanacloud-prom',  'metrics written by infra/alloy'),
    'LOKI':  ('grafanacloud-logs',  'logs written by infra/alloy'),
    'USAGE': ('grafanacloud-usage', 'Grafana Cloud billing/usage series'),
}
by_uid = {d['uid']: d for d in ds}

for var, (uid, what) in WANT.items():
    d = by_uid.get(uid)
    print(f"{var}_UID={shlex.quote(d['uid'] if d else '')}")
    print(f"{var}_NAME={shlex.quote(d['name'] if d else '')}")
    print(f"{var}_WHAT={shlex.quote(what)}")

# Surface the near-misses so a stack that names things differently is obvious
# rather than mysterious.
for var, (uid, _) in WANT.items():
    if uid not in by_uid:
        kind = 'loki' if var == 'LOKI' else 'prometheus'
        others = [f"{d['uid']} ({d['name']})" for d in ds if d.get('type') == kind]
        print(f"{var}_CANDIDATES={shlex.quote(', '.join(others) or 'none')}")
PY
)"

if [[ -z "$PROM_UID" ]]; then
  echo "FAIL: no datasource with uid 'grafanacloud-prom' in this Grafana."
  echo "      Prometheus datasources present: ${PROM_CANDIDATES:-none}"
  exit 1
fi
if [[ -z "$LOKI_UID" ]]; then
  echo "FAIL: no datasource with uid 'grafanacloud-logs' in this Grafana."
  echo "      Loki datasources present: ${LOKI_CANDIDATES:-none}"
  echo
  echo "      Do NOT just take the first Loki datasource: a Cloud stack has"
  echo "      three, and 'alert-state-history' and 'usage-insights' both accept"
  echo "      queries happily while containing none of the cluster's logs."
  exit 1
fi
echo "    prometheus : ${PROM_NAME} (${PROM_UID})"
echo "    loki       : ${LOKI_NAME} (${LOKI_UID})"
if [[ -n "$USAGE_UID" ]]; then
  echo "    usage      : ${USAGE_NAME} (${USAGE_UID})"
else
  echo "    usage      : NOT FOUND — the series-budget panels will be empty."
  echo "                 That datasource is created by Grafana Cloud itself;"
  echo "                 its absence means this is not a Cloud stack."
fi

substitute() {
  sed -e "s|__PROM_UID__|${PROM_UID}|g" \
      -e "s|__LOKI_UID__|${LOKI_UID}|g" \
      -e "s|__USAGE_UID__|${USAGE_UID}|g" "$1"
}

# ---------------------------------------------------------------------------
# Validate before pushing anything
# ---------------------------------------------------------------------------
# Same rule CI applies (see .github/workflows/lint.yml): every dashboard must
# parse and must carry a stable uid. Without a uid, each push CREATES a new
# dashboard instead of updating the existing one, and you end up with six copies
# of "Edge Traffic" and no idea which one is live.
echo "==> validating dashboard JSON"
python3 - "$DASHBOARD_DIR" <<'PY'
import json, pathlib, sys
bad = 0
uids = {}
for f in sorted(pathlib.Path(sys.argv[1]).glob('*.json')):
    try:
        d = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        print(f"    INVALID JSON {f.name}: {e}"); bad += 1; continue
    uid = d.get('uid')
    if not uid:
        print(f"    MISSING uid  {f.name}"); bad += 1; continue
    if uid in uids:
        print(f"    DUPLICATE uid '{uid}' in {f.name} and {uids[uid]}"); bad += 1; continue
    uids[uid] = f.name
    print(f"    ok {f.name:<26} uid={uid:<22} panels={len(d.get('panels', []))}")
sys.exit(1 if bad else 0)
PY

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "==> --dry-run: nothing pushed"
  exit 0
fi

# ---------------------------------------------------------------------------
# Folder
# ---------------------------------------------------------------------------
FOLDER_UID="selfhosted"
if [[ "$(api_code GET "/api/folders/${FOLDER_UID}")" != "200" ]]; then
  echo "==> creating folder '${FOLDER_TITLE}'"
  BODY="$(mktemp)"; TMP_FILES+=("$BODY")
  printf '{"uid":"%s","title":"%s"}' "$FOLDER_UID" "$FOLDER_TITLE" >"$BODY"
  api POST /api/folders "$BODY" >/dev/null
fi

# ---------------------------------------------------------------------------
# Push dashboards
# ---------------------------------------------------------------------------
echo "==> pushing dashboards"
PUSHED=()
for f in "$DASHBOARD_DIR"/*.json; do
  [[ -e "$f" ]] || continue
  name="$(basename "$f")"
  BODY="$(mktemp)"; TMP_FILES+=("$BODY")

  # overwrite:true is what makes a re-push an update rather than a conflict.
  # The dashboard's own `version` is stripped for the same reason: keeping it
  # turns every push after a UI edit into a 412 version mismatch.
  substitute "$f" | python3 -c "
import json, sys
d = json.load(sys.stdin)
d.pop('version', None)
d.pop('id', None)
json.dump({'dashboard': d, 'folderUid': '${FOLDER_UID}', 'overwrite': True}, sys.stdout)
" >"$BODY"

  RESP="$(api POST /api/dashboards/db "$BODY")"
  if grep -q '"status":"success"' <<<"$RESP"; then
    URL="$(python3 -c "import json,sys; print(json.load(sys.stdin).get('url',''))" <<<"$RESP")"
    echo "    ok ${name}  ${GRAFANA_URL}${URL}"
    PUSHED+=("$f")
  else
    echo "    FAIL ${name}: $(head -c 400 <<<"$RESP")"
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# Alert rules
# ---------------------------------------------------------------------------
# Provisioned one at a time: the API takes a single rule per POST, and a partial
# failure is far easier to read this way than one 400 covering twelve rules.
#
# Rules are matched by uid — an existing uid is a PUT, a new one a POST — so
# re-running this converges rather than duplicating.
if [[ -f "$ALERT_FILE" ]]; then
  # THE CONTACT POINT IS DELIBERATELY A DEAD END, and it has to exist.
  #
  # Every rule sets notification_settings.receiver = "unwired". Without that
  # receiver, rules fall through to Grafana's DEFAULT notification policy, which
  # on a Cloud stack means email — so "I only defined the rules, nothing is
  # wired up" would quietly start mailing on the first firing rule.
  #
  # 127.0.0.1:9 is the standard discard port: the connection is refused
  # instantly, on the loopback interface, with no DNS lookup and no packet
  # leaving the machine. Rules evaluate and show their state in the UI;
  # notifications go nowhere.
  #
  # TO MAKE IT LIVE, change this one block to sms-relay and re-run:
  #   "url": "https://sms-relay.zachd.duckdns.org/api/v1/messages"
  #   plus an X-API-Key header — matching scripts/notify-failure.sh, which is
  #   how the systemd timers already text on failure.
  echo "==> ensuring the 'unwired' contact point exists"
  CP_BODY="$(mktemp)"; TMP_FILES+=("$CP_BODY")
  cat >"$CP_BODY" <<'JSON'
{
  "uid": "unwired",
  "name": "unwired",
  "type": "webhook",
  "disableResolveMessage": true,
  "settings": { "url": "http://127.0.0.1:9/", "httpMethod": "POST" }
}
JSON
  if [[ "$(api_code GET /api/v1/provisioning/contact-points/unwired)" == "200" ]]; then
    api PUT /api/v1/provisioning/contact-points/unwired "$CP_BODY" >/dev/null
  else
    api POST /api/v1/provisioning/contact-points "$CP_BODY" >/dev/null
  fi
  echo "    ok: notifications route to a discard port, not to you"

  echo "==> provisioning alert rules"
  RULE_COUNT="$(python3 -c "import json;print(len(json.load(open('${ALERT_FILE}'))))")"
  for i in $(seq 0 $((RULE_COUNT - 1))); do
    BODY="$(mktemp)"; TMP_FILES+=("$BODY")
    substitute "$ALERT_FILE" | python3 -c "
import json, sys
rules = json.load(sys.stdin)
r = rules[${i}]
r['folderUID'] = '${FOLDER_UID}'
json.dump(r, sys.stdout)
" >"$BODY"
    RUID="$(python3 -c "import json;print(json.load(open('${BODY}'))['uid'])")"
    RTITLE="$(python3 -c "import json;print(json.load(open('${BODY}'))['title'])")"

    if [[ "$(api_code GET "/api/v1/provisioning/alert-rules/${RUID}")" == "200" ]]; then
      RESP="$(api PUT "/api/v1/provisioning/alert-rules/${RUID}" "$BODY")"
    else
      RESP="$(api POST /api/v1/provisioning/alert-rules "$BODY")"
    fi

    if grep -q '"uid"' <<<"$RESP"; then
      echo "    ok ${RTITLE}"
    else
      echo "    FAIL ${RTITLE}: $(head -c 300 <<<"$RESP")"
      exit 1
    fi
  done
fi

# ---------------------------------------------------------------------------
# --verify: does every panel actually return data?
# ---------------------------------------------------------------------------
# A dashboard that pushes cleanly and shows twelve empty panels is worse than no
# dashboard, because it reads as "nothing is happening" rather than "this query
# is wrong". So run each panel's query through the same datasource the panel
# uses and report what came back empty.
#
# Emptiness is not automatically a bug — "no CrashLooping pods" is good news.
# Panels whose description contains [may-be-empty] are expected to be empty when
# things are healthy, and are reported separately rather than as failures.
if [[ "$VERIFY" -eq 1 ]]; then
  echo "==> verifying panel queries against live data"
  for f in "${PUSHED[@]}"; do
    QUERIES="$(mktemp)"; TMP_FILES+=("$QUERIES")
    substitute "$f" >"$QUERIES"
    python3 - "$QUERIES" "$GRAFANA_URL" "$AUTH_HEADER_FILE" <<'PY'
import json, subprocess, sys, time

dash_file, url, auth_file = sys.argv[1], sys.argv[2], sys.argv[3]
dash = json.load(open(dash_file))
token = open(auth_file).read().split('Bearer ', 1)[1].strip()

WINDOW_H = 6
now = int(time.time() * 1000)
frm = now - WINDOW_H * 3600 * 1000

# /api/ds/query is the raw datasource API: it does NO dashboard-variable
# interpolation. Sending a panel's expr verbatim means querying for the literal
# string `cluster="$cluster"`, which matches nothing — so every panel reports
# empty and the whole check quietly becomes a no-op that always "fails".
# Substitute the same way Grafana would before rendering.
VARS = {}
for v in (dash.get('templating') or {}).get('list') or []:
    cur = (v.get('current') or {}).get('value')
    if isinstance(cur, str):
        VARS[v['name']] = cur

def interpolate(expr):
    for name, val in VARS.items():
        expr = expr.replace('${%s}' % name, val).replace('$' + name, val)
    # Built-ins Grafana would resolve from the panel's time range.
    expr = expr.replace('$__range', f'{WINDOW_H}h')
    expr = expr.replace('$__rate_interval', '5m').replace('$__interval', '1m')
    return expr

def walk(panels):
    for p in panels:
        if p.get('type') == 'row':
            yield from walk(p.get('panels') or [])
        else:
            yield p

empty, soft, errors, ok = [], [], [], 0

for p in walk(dash.get('panels') or []):
    if p.get('type') in ('text', 'row'):
        continue
    lenient = '[may-be-empty]' in (p.get('description') or '')
    for t in p.get('targets') or []:
        expr = t.get('expr')
        if not expr:
            continue
        expr = interpolate(expr)
        ds = t.get('datasource') or p.get('datasource') or {}
        q = {
            'refId': t.get('refId', 'A'),
            'datasource': ds,
            'expr': expr,
            'intervalMs': 60000,
            'maxDataPoints': 100,
        }
        if ds.get('type') == 'loki':
            q['queryType'] = t.get('queryType', 'range')
        body = json.dumps({'from': str(frm), 'to': str(now), 'queries': [q]})
        out = subprocess.run(
            ['curl', '-sS', '-X', 'POST', '-H', f'Authorization: Bearer {token}',
             '-H', 'Content-Type: application/json', '--data-binary', '@-',
             f'{url}/api/ds/query'],
            input=body, capture_output=True, text=True).stdout
        label = f"{p.get('title', '?')} [{t.get('refId', 'A')}]"
        try:
            res = json.loads(out)['results'][q['refId']]
        except Exception:
            errors.append((label, out[:160])); continue
        if res.get('error'):
            errors.append((label, res['error'][:160])); continue
        frames = res.get('frames') or []
        rows = sum(len((fr.get('data') or {}).get('values') or [[]]) and
                   len(((fr.get('data') or {}).get('values') or [[]])[0]) for fr in frames)
        if rows:
            ok += 1
        elif lenient:
            soft.append(label)
        else:
            empty.append(label)

n = dash.get('title', dash_file)
print(f"    {n}: {ok} panels returning data")
for label, msg in errors:
    print(f"      QUERY ERROR  {label}: {msg}")
for label in empty:
    print(f"      EMPTY        {label}")
for label in soft:
    print(f"      empty (ok)   {label}")
sys.exit(1 if errors else 0)
PY
  done
fi

echo
echo "Done. Dashboards: ${GRAFANA_URL}/dashboards/f/${FOLDER_UID}"
