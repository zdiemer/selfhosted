#!/usr/bin/env bash
# Apply the current chart to the crowdsec-machine-prune release.
#
#   ./upgrade.sh              install/update the CronJob
#   ./upgrade.sh --run-now    install, then trigger a run and show the result
#
# Deploying this changes nothing about how CrowdSec detects or bans. It only
# deletes machine registrations whose agent pod no longer exists — see
# values.yaml for why those are not merely untidy.
#
# Ordering: infra/crowdsec must be installed first, because this job execs into
# the LAPI pod that chart creates. That is checked below rather than remembered.

set -euo pipefail

RELEASE="${RELEASE:-crowdsec-machine-prune}"
NAMESPACE="${NAMESPACE:-crowdsec}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"

RUN_NOW=false
[[ "${1:-}" == "--run-now" ]] && RUN_NOW=true

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# The LAPI Deployment is this job's entire reason to exist and its only
# dependency. A CronJob that fails nightly for want of it is worse than a
# refusal here, because nightly failure is the kind of alarm people learn to
# ignore.
LAPI_DEPLOY="$(python3 -c "
import yaml; print(yaml.safe_load(open('${VALUES}'))['lapi']['deployment'])")"
if ! $K get deploy "$LAPI_DEPLOY" >/dev/null 2>&1; then
  echo "FAIL: Deployment ${LAPI_DEPLOY} not found in ${NAMESPACE}."
  echo "      It belongs to infra/crowdsec — run that chart's upgrade.sh first."
  echo "      If the release was renamed, update lapi.deployment in values.yaml."
  exit 1
fi

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" -f "$VALUES" \
  --cleanup-on-fail

echo "==> CronJob"
$K get cronjob "$RELEASE"

echo "==> machines currently registered"
$K exec "deploy/${LAPI_DEPLOY}" -- cscli machines list 2>/dev/null | sed 's/^/    /'

if [[ "$RUN_NOW" == "true" ]]; then
  JOB="${RELEASE}-manual-$(date +%s)"
  echo "==> triggering ${JOB} from the CronJob"
  $K create job "$JOB" --from="cronjob/${RELEASE}"
  # One exec into a running pod. If this is not done in two minutes something
  # is wrong with the LAPI, not with the wait.
  $K wait --for=condition=complete --timeout=120s "job/${JOB}" \
    || { echo "FAIL: job did not complete. Logs:"; $K logs "job/${JOB}" --tail=100; exit 1; }
  echo "==> logs"
  $K logs "job/${JOB}" --tail=100
fi
