#!/usr/bin/env bash
# helm lint + helm template every first-party chart with its tracked values.
# Charts whose templates `required`-gate on values.local.yaml secrets can't
# render from tracked values alone and are listed in TEMPLATE_SKIP (lint still
# runs for them — LINT_SKIP is the smaller set whose lint itself needs
# secrets or gitignored subchart tarballs). Submodule charts are skipped:
# they have their own repos/CI and aren't present in a non-recursive checkout.

set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

# lint renders too, so secret-gated + vendored-dependency charts can't lint
# from a clean checkout (Chart.lock/charts/*.tgz are gitignored local state).
LINT_SKIP=(
  infra/democratic-csi
  infra/k8up
  infra/renovate
)

# `required` secrets in values.local.yaml block a default render.
TEMPLATE_SKIP=(
  auth/authelia
  auth/keepass
  dev/happy-server
  discord/vocard
  docs/paperless-ngx
  games/romm
  infra/cloudflared
  infra/democratic-csi
  infra/duckdns
  infra/egress-proxy
  infra/k8up
  infra/renovate
  media/arr
  web/apartment-watch
  web/kelsey-green
)

in_list() {
  local needle=$1; shift
  local x
  for x in "$@"; do [[ $x == "$needle" ]] && return 0; done
  return 1
}

submodules=$(git config --file .gitmodules --get-regexp path | awk '{print $2}')
is_submodule() {
  local d=$1 s
  for s in $submodules; do
    [[ $d == "$s" || $d == "$s"/* ]] && return 0
  done
  return 1
}

fail=0
linted=0
templated=0
for chart_yaml in $(find . -mindepth 3 -maxdepth 4 -name Chart.yaml -not -path "*/charts/*" | sort); do
  d=${chart_yaml#./}
  d=${d%/Chart.yaml}
  is_submodule "$d" && continue

  if ! in_list "$d" "${LINT_SKIP[@]}"; then
    if ! helm lint "$d" --quiet >/tmp/lint-out 2>&1; then
      echo "FAIL helm lint $d"; cat /tmp/lint-out; fail=1
    fi
    linted=$((linted + 1))
  fi

  if ! in_list "$d" "${TEMPLATE_SKIP[@]}"; then
    if ! helm template "$d" -f "$d/values.yaml" >/dev/null 2>/tmp/tmpl-out; then
      echo "FAIL helm template $d"; cat /tmp/tmpl-out; fail=1
    fi
    templated=$((templated + 1))
  fi
done

echo "helm lint: ${linted} charts, helm template: ${templated} charts"
exit $fail
