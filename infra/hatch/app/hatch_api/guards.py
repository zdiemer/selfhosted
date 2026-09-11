"""The single choke point for every 403.

Both deny-lists live here — which namespaces' logs may be read, and which
workloads may be mutated — so there is one place to read to know what hatch is
allowed to do, and one place a test can exercise.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch

from hatch_api.config import settings


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str = "ok"
    reason: str = ""
    matched_rule: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "matchedRule": self.matched_rule,
        }


ALLOW = Decision(allowed=True)


def _matches(entry: str, kind: str, namespace: str, name: str) -> bool:
    """Match one deny-list entry against a target.

    Three forms, all fnmatch-globbed:
        name                  any kind, any namespace
        namespace/name        both must match
        Kind:namespace/name   all three, kind compared case-insensitively
    """
    want_kind = None
    if ":" in entry:
        want_kind, entry = entry.split(":", 1)

    if "/" in entry:
        want_ns, want_name = entry.split("/", 1)
    else:
        want_ns, want_name = "*", entry

    if want_kind is not None and not fnmatch(kind.lower(), want_kind.strip().lower()):
        return False
    return fnmatch(namespace, want_ns) and fnmatch(name, want_name)


def check_logs(namespace: str) -> Decision:
    if not settings.logs_enabled:
        return Decision(
            allowed=False,
            code="logs_disabled",
            reason="pod log access is disabled on this deployment",
        )
    if namespace in settings.logs_deny_namespaces:
        return Decision(
            allowed=False,
            code="namespace_protected",
            reason=(
                f"pod logs in namespace '{namespace}' are not readable: log text is "
                "unbounded application output and this namespace handles credentials "
                "or personal data"
            ),
            matched_rule=f"logs.denyNamespaces: {namespace}",
        )
    return ALLOW


def check_action(action: str, kind: str, namespace: str, name: str) -> Decision:
    """Evaluate an action against every guard, BEFORE any API call is made.

    TARGET rules are evaluated before the enabled/disabled switches, and that
    ordering is deliberate. Both outcomes are a denial, so nothing is lost
    safety-wise, but it means POST /v1/actions/check reports "traefik is
    protected" rather than the useless "actions are disabled" during the
    read-only phases — which is exactly when you want to verify the deny-list
    behaves before turning mutations on.
    """
    if namespace in settings.actions_deny_namespaces:
        return Decision(
            allowed=False,
            code="namespace_protected",
            reason=f"namespace '{namespace}' is protected from all actions",
            matched_rule=f"actions.denyNamespaces: {namespace}",
        )

    for entry in settings.actions_deny_workloads:
        if _matches(entry, kind, namespace, name):
            return Decision(
                allowed=False,
                code="workload_protected",
                reason=(
                    f"{kind} {namespace}/{name} is protected: acting on it would break "
                    "the cluster or sever hatch's own access while it is diagnosing"
                ),
                matched_rule=f"actions.denyWorkloads: {entry}",
            )

    # Independent of the chart-injected self-deny above. Belt and braces,
    # because the failure is specific: restarting itself kills the request
    # mid-flight, so no result line is ever written and the caller sees a
    # connection reset it cannot distinguish from a crash.
    if namespace == settings.namespace and name == settings.release:
        return Decision(
            allowed=False,
            code="self_protected",
            reason="hatch will not act on itself",
            matched_rule="self",
        )

    # Report a binding gap locally rather than letting it surface as an opaque
    # 403 from the API server, which an agent cannot tell apart from a bug.
    if (
        settings.rbac_act_mode == "namespaced"
        and namespace not in settings.rbac_act_allow_namespaces
    ):
        return Decision(
            allowed=False,
            code="namespace_not_bound",
            reason=(
                f"no RoleBinding grants hatch mutation rights in '{namespace}'; "
                "add it to rbac.act.allowNamespaces to permit this"
            ),
            matched_rule=f"rbac.act.allowNamespaces: {sorted(settings.rbac_act_allow_namespaces)}",
        )

    # The switches last, so a protected target reports why it is protected.
    if not settings.actions_enabled:
        return Decision(
            allowed=False,
            code="actions_disabled",
            reason="actions are disabled on this deployment (actions.enabled=false)",
            matched_rule="actions.enabled",
        )

    enabled = {
        "restart": settings.actions_restart_enabled,
        "delete-pod": settings.actions_delete_pod_enabled,
        "scale": settings.actions_scale_enabled,
    }
    if not enabled.get(action, False):
        return Decision(
            allowed=False,
            code="action_disabled",
            reason=f"action '{action}' is disabled on this deployment",
            matched_rule=f"actions.{action}.enabled",
        )

    return ALLOW
