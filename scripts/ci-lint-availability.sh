#!/usr/bin/env bash
# Enforce the availability conventions from the root README across every
# first-party chart, by rendering each one and inspecting the result.
#
# WHY A RENDER-TIME CHECK
# These conventions are the kind that fail silently. A `preferred`
# podAntiAffinity that the scheduler declines still looks correct in the values
# file; a `strategy:` key the chart doesn't read renders nothing at all and
# nobody notices until a world gets corrupted. Grepping the templates would
# reproduce exactly that blind spot — only the rendered manifest says what will
# actually exist in the cluster.
#
# WHAT IT CHECKS
#   1. replicas >= 2  ->  needs a PodDisruptionBudget AND a spread constraint.
#      Running two copies for availability means nothing if a drain can take
#      both, or if the scheduler put them on one node to begin with.
#   2. replicas == 1  ->  must NOT have a PDB. A minAvailable: 1 budget against
#      a single replica can never be satisfied by evicting it, so the drain
#      blocks forever. This is the one rule here that catches a mistake which
#      is strictly worse than the gap it was trying to prevent.
#   3. A container with a readinessProbe (the usable proxy for "something
#      routes to this") needs a preStop hook, because endpoint removal races
#      SIGTERM.
#   4. A pod with a preStop needs an explicit terminationGracePeriodSeconds
#      strictly greater than the sleep, or the kubelet hard-kills during the
#      sleep and the hook accomplishes nothing.
#
# Charts whose templates `required`-gate on values.local.yaml secrets cannot
# render from tracked values alone; they are checked only when that file is
# present (i.e. locally, not in CI). Same trade ci-lint-charts.sh makes.

set -uo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

command -v helm >/dev/null || { echo "helm required" >&2; exit 1; }

submodules=$(git config --file .gitmodules --get-regexp path 2>/dev/null | awk '{print $2}')
is_submodule() {
    local d=$1 s
    for s in $submodules; do
        [[ $d == "$s" || $d == "$s"/* ]] && return 0
    done
    return 1
}

fail=0
checked=0
skipped=0

for chart_yaml in $(find . -mindepth 3 -maxdepth 4 -name Chart.yaml -not -path "*/charts/*" | sort); do
    d=${chart_yaml#./}
    d=${d%/Chart.yaml}
    is_submodule "$d" && continue

    args=(-f "$d/values.yaml")
    [[ -f "$d/values.local.yaml" ]] && args+=(-f "$d/values.local.yaml")

    if ! rendered=$(helm template lint-availability "$d" "${args[@]}" 2>/dev/null); then
        skipped=$((skipped + 1))
        continue
    fi
    checked=$((checked + 1))

    out=$(printf '%s' "$rendered" | python3 scripts/lib/check-availability.py "$d" 2>&1)

    if [[ -n "$out" ]]; then
        echo "$out"
        fail=1
    fi
done

echo
if [[ $fail -eq 0 ]]; then
    echo "availability policy: OK (${checked} charts checked, ${skipped} unrenderable without secrets)"
else
    echo "availability policy: FAILED (${checked} charts checked, ${skipped} unrenderable without secrets)"
fi
exit $fail
