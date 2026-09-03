"""Environment into typed settings, parsed once at import.

Everything the chart can configure arrives as an env var so that `kubectl set
env` and a values edit reach the same place, and so nothing has to be read off
disk at request time.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _json_list(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # A comma-separated fallback, so a hand-set env var during debugging
        # does not silently become an empty deny-list.
        return [p.strip() for p in raw.split(",") if p.strip()]
    return [str(p) for p in parsed] if isinstance(parsed, list) else []


@dataclass(frozen=True)
class ApiKey:
    name: str
    key: str
    scopes: frozenset[str]


def _parse_api_keys(raw: str) -> list[ApiKey]:
    """Parse HATCH_API_KEYS.

    Canonical form is a JSON object of caller -> {key, scopes}:

        {"hatch": {"key": "…", "scopes": ["read", "act"]}}

    A bare caller -> key mapping is also accepted and means both scopes, which
    is the single-token setup. Splitting later is then a vault edit rather than
    a code change.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("HATCH_API_KEYS must be a JSON object of caller -> key")

    keys: list[ApiKey] = []
    for name, spec in parsed.items():
        if isinstance(spec, str):
            key, scopes = spec, {"read", "act"}
        elif isinstance(spec, dict):
            key = str(spec.get("key", ""))
            scopes = set(spec.get("scopes") or ["read", "act"])
        else:
            raise ValueError(f"api key {name!r}: expected a string or an object")
        if not key:
            raise ValueError(f"api key {name!r}: empty key")
        unknown = scopes - {"read", "act"}
        if unknown:
            raise ValueError(f"api key {name!r}: unknown scopes {sorted(unknown)}")
        keys.append(ApiKey(name=name, key=key, scopes=frozenset(scopes)))
    return keys


@dataclass(frozen=True)
class Settings:
    port: int
    namespace: str
    release: str
    pod_name: str

    api_keys: list[ApiKey]
    accept_x_api_key: bool

    logs_enabled: bool
    logs_deny_namespaces: frozenset[str]
    logs_max_tail_lines: int
    logs_default_tail_lines: int
    logs_max_bytes: int
    logs_redact_enabled: bool
    logs_redact_patterns: list[re.Pattern[str]]

    cluster_status_enabled: bool
    cluster_status_url: str
    cluster_status_cache_seconds: int
    cluster_status_timeout_seconds: int
    cluster_status_default_sections: list[str]

    grafana_enabled: bool
    grafana_url: str
    grafana_token: str
    grafana_prom_uid: str
    grafana_loki_uid: str
    grafana_verify_datasources: bool
    grafana_query_mode: str
    grafana_timeout_seconds: int
    grafana_max_range_points: int
    grafana_max_lookback_hours: int
    grafana_logs_default_limit: int
    grafana_logs_max_limit: int

    actions_enabled: bool
    actions_restart_enabled: bool
    actions_delete_pod_enabled: bool
    actions_scale_enabled: bool
    actions_scale_min: int
    actions_scale_max: int
    actions_deny_workloads: list[str]
    actions_deny_namespaces: frozenset[str]
    actions_cooldown_enabled: bool
    actions_cooldown_seconds: int
    actions_notify_enabled: bool
    actions_notify_url: str
    actions_notify_events: frozenset[str]

    rbac_act_mode: str
    rbac_act_allow_namespaces: frozenset[str]

    audit_ring_size: int
    audit_source: str

    key_errors: list[str] = field(default_factory=list)


def load() -> Settings:
    key_errors: list[str] = []
    try:
        api_keys = _parse_api_keys(os.environ.get("HATCH_API_KEYS", ""))
    except (ValueError, json.JSONDecodeError) as exc:
        api_keys = []
        key_errors.append(str(exc))

    patterns: list[re.Pattern[str]] = []
    for pat in _json_list("HATCH_LOGS_REDACT_PATTERNS"):
        try:
            patterns.append(re.compile(pat))
        except re.error as exc:
            # A bad regex must not take the service down, but it must be loud:
            # a redaction pattern that silently does not compile is a leak.
            key_errors.append(f"log redaction pattern {pat!r} did not compile: {exc}")

    return Settings(
        port=_int("HATCH_PORT_NUMBER", 8080),
        namespace=os.environ.get("HATCH_RELEASE_NAMESPACE", "infra"),
        release=os.environ.get("HATCH_RELEASE_NAME", "hatch"),
        pod_name=os.environ.get("HATCH_POD_NAME", "local"),
        api_keys=api_keys,
        accept_x_api_key=_bool("HATCH_ACCEPT_X_API_KEY", True),
        logs_enabled=_bool("HATCH_LOGS_ENABLED", True),
        logs_deny_namespaces=frozenset(_json_list("HATCH_LOGS_DENY_NAMESPACES")),
        logs_max_tail_lines=_int("HATCH_LOGS_MAX_TAIL_LINES", 2000),
        logs_default_tail_lines=_int("HATCH_LOGS_DEFAULT_TAIL_LINES", 200),
        logs_max_bytes=_int("HATCH_LOGS_MAX_BYTES", 262144),
        logs_redact_enabled=_bool("HATCH_LOGS_REDACT_ENABLED", True),
        logs_redact_patterns=patterns,
        cluster_status_enabled=_bool("HATCH_CLUSTER_STATUS_ENABLED", True),
        cluster_status_url=os.environ.get("HATCH_CLUSTER_STATUS_URL", ""),
        cluster_status_cache_seconds=_int("HATCH_CLUSTER_STATUS_CACHE_SECONDS", 15),
        cluster_status_timeout_seconds=_int("HATCH_CLUSTER_STATUS_TIMEOUT_SECONDS", 5),
        cluster_status_default_sections=_json_list("HATCH_CLUSTER_STATUS_DEFAULT_SECTIONS"),
        grafana_enabled=_bool("HATCH_GRAFANA_ENABLED", False),
        grafana_url=os.environ.get("HATCH_GRAFANA_URL", "").rstrip("/"),
        grafana_token=os.environ.get("HATCH_GRAFANA_TOKEN", "").strip(),
        grafana_prom_uid=os.environ.get("HATCH_GRAFANA_PROM_UID", "grafanacloud-prom"),
        grafana_loki_uid=os.environ.get("HATCH_GRAFANA_LOKI_UID", "grafanacloud-logs"),
        grafana_verify_datasources=_bool("HATCH_GRAFANA_VERIFY_DATASOURCES", True),
        grafana_query_mode=os.environ.get("HATCH_GRAFANA_QUERY_MODE", "auto"),
        grafana_timeout_seconds=_int("HATCH_GRAFANA_TIMEOUT_SECONDS", 20),
        grafana_max_range_points=_int("HATCH_GRAFANA_MAX_RANGE_POINTS", 200),
        grafana_max_lookback_hours=_int("HATCH_GRAFANA_MAX_LOOKBACK_HOURS", 168),
        grafana_logs_default_limit=_int("HATCH_GRAFANA_LOGS_DEFAULT_LIMIT", 200),
        grafana_logs_max_limit=_int("HATCH_GRAFANA_LOGS_MAX_LIMIT", 1000),
        actions_enabled=_bool("HATCH_ACTIONS_ENABLED", False),
        actions_restart_enabled=_bool("HATCH_ACTIONS_RESTART_ENABLED", True),
        actions_delete_pod_enabled=_bool("HATCH_ACTIONS_DELETE_POD_ENABLED", True),
        actions_scale_enabled=_bool("HATCH_ACTIONS_SCALE_ENABLED", True),
        actions_scale_min=_int("HATCH_ACTIONS_SCALE_MIN", 0),
        actions_scale_max=_int("HATCH_ACTIONS_SCALE_MAX", 10),
        actions_deny_workloads=_json_list("HATCH_ACTIONS_DENY_WORKLOADS"),
        actions_deny_namespaces=frozenset(_json_list("HATCH_ACTIONS_DENY_NAMESPACES")),
        actions_cooldown_enabled=_bool("HATCH_ACTIONS_COOLDOWN_ENABLED", False),
        actions_cooldown_seconds=_int("HATCH_ACTIONS_COOLDOWN_SECONDS", 300),
        actions_notify_enabled=_bool("HATCH_ACTIONS_NOTIFY_ENABLED", False),
        actions_notify_url=os.environ.get("HATCH_ACTIONS_NOTIFY_URL", ""),
        actions_notify_events=frozenset(_json_list("HATCH_ACTIONS_NOTIFY_EVENTS")),
        rbac_act_mode=os.environ.get("HATCH_RBAC_ACT_MODE", "namespaced"),
        rbac_act_allow_namespaces=frozenset(_json_list("HATCH_RBAC_ACT_ALLOW_NAMESPACES")),
        audit_ring_size=_int("HATCH_AUDIT_RING_SIZE", 500),
        audit_source=os.environ.get("HATCH_AUDIT_SOURCE", "memory"),
        key_errors=key_errors,
    )


settings = load()
