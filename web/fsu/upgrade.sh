#!/usr/bin/env bash
# Deploy fsu.zachd.duckdns.org.
#
# NO sv_load, and that is deliberate rather than an omission. There are no
# secrets anywhere in this chart: the in-cluster registry is anonymously
# pullable from every node, nothing here talks to an external API, and the
# pybank demo password is fake money on a tailnet-only host that is reseeded on
# every pod start. Sourcing scripts/lib/secret-values.sh would add a 1Password
# dependency to a deploy that has none and make sv_has permanently false.
#
# If that ever changes, the pattern is infra/hatch/upgrade.sh: source
# ../../scripts/lib/secret-values.sh, add a values.local.tpl.yaml, and pass
# -f <(sv_fd).

set -euo pipefail

RELEASE="${RELEASE:-fsu}"
NAMESPACE="${NAMESPACE:-fsu}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VALUES="${HERE}/values.yaml"

K="kubectl -n ${NAMESPACE}"

command -v helm    >/dev/null || { echo "helm required"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# A dedicated namespace, not the shared `web` one, because one of these
# workloads is a real shell: the NetworkPolicy here is default-deny, and in
# `web` that would silently land on four other charts whose authors never opted
# in. infra/hatch/upgrade.sh is the precedent for creating it here.
$K get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

echo "==> helm upgrade --install ${RELEASE} ${HERE} -n ${NAMESPACE}"
helm upgrade --install "$RELEASE" "$HERE" -n "$NAMESPACE" \
    -f "$VALUES" --atomic --cleanup-on-fail "$@"

for d in web terminal pybank; do
    if $K get deploy "${RELEASE}-${d}" >/dev/null 2>&1; then
        echo "==> Waiting for ${RELEASE}-${d} rollout"
        $K rollout status "deployment/${RELEASE}-${d}" --timeout=180s
    fi
done

echo "==> Pods"
$K get pods -l app.kubernetes.io/instance="$RELEASE"

# ---------------------------------------------------------------------------
# Post-deploy assertions.
#
# The HTTP ones WARN rather than fail when they cannot reach the host, because
# *.zachd.duckdns.org resolves to a 100.x Tailscale address and a deploy run
# from off-tailnet legitimately cannot see it (the infra/hatch pattern). The
# declarative ones have no such excuse and do fail.
HOST="$($K get ingress "$RELEASE" -o jsonpath='{.spec.rules[0].host}' 2>/dev/null || true)"
echo
echo "==> Checks"

fail=0

# 1. THE ONE THAT MATTERS.
#    If /cloysta answers 200 instead of redirecting to Authelia, the middleware
#    annotation did not take and a fork/exec shell is open to anything on the
#    tailnet. Everything below this is a nicety; this is not.
if [[ -n "$HOST" ]]; then
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://${HOST}/cloysta/" || echo 000)"
    case "$code" in
        3*)   echo "    ok:   /cloysta -> ${code} (Authelia)" ;;
        000)  echo "    WARN: /cloysta unreachable — expected if you are off the tailnet" ;;
        200)  echo "    FAIL: /cloysta returned 200. The forwardAuth middleware is NOT applied."
              echo "          A shell is exposed to the whole tailnet. Check the"
              echo "          router.middlewares annotation on ingress ${RELEASE}-terminal."
              fail=1 ;;
        *)    echo "    FAIL: /cloysta returned ${code}, expected a 3xx redirect"; fail=1 ;;
    esac

    # 2. The unauthenticated half really is unauthenticated.
    curl -s -o /dev/null -w '    /healthz    %{http_code}\n' --max-time 15 "https://${HOST}/healthz" || true

    # 3. The pybank prefix resolved. See settings_shim.py: if somebody swaps the
    #    URLconf shim back for StripPrefix + FORCE_SCRIPT_NAME, the page still
    #    returns 200 and every stylesheet 404s — so check the stylesheet, not
    #    the page.
    curl -s -o /dev/null -w '    pybank css  %{http_code}\n' --max-time 15 \
        "https://${HOST}/pybank/static/main/uikit.min.css" || true
    if curl -s --max-time 15 "https://${HOST}/pybank/main/" | grep -q '/pybank/static/'; then
        echo "    ok:   {% static %} emits the /pybank prefix"
    fi
fi

# 4. Assert the negative space, declaratively — no exec into the shell needed.
if $K get deploy "${RELEASE}-terminal" >/dev/null 2>&1; then
    $K get netpol "${RELEASE}-terminal" >/dev/null 2>&1 \
        || { echo "    FAIL: the terminal NetworkPolicy is missing"; fail=1; }

    amt="$($K get pod -l app.kubernetes.io/component=terminal \
             -o jsonpath='{.items[0].spec.automountServiceAccountToken}' 2>/dev/null || true)"
    [[ "$amt" == "false" ]] \
        || { echo "    FAIL: terminal pod mounts a service account token (got '${amt}')"; fail=1; }

    [[ $fail -eq 0 ]] && echo "    ok:   netpol present, no service-account token"
fi

exit "$fail"
