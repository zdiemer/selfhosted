"""Shared test environment for the bridge.

bridge.py builds its `CFG` at import time via `Config.from_env()` and imports
the kubernetes / mctools clients at module scope, neither of which is needed
by the pure functions under test. So the env has to be populated and both
clients stubbed before the module loads — conftest is imported first, which
makes this the only place that can happen.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types

TMPDIR = tempfile.mkdtemp(prefix="claude-bridge-test-")

os.environ.setdefault("MC_NAMESPACE", "minecraft")
os.environ.setdefault("MC_POD_SELECTOR", "app=mc-minecraft")
os.environ.setdefault("MC_POD_CONTAINER", "mc-minecraft")
os.environ.setdefault("RCON_HOST", "localhost")
os.environ.setdefault("RCON_PASSWORD", "test")
os.environ.setdefault("SYSTEM_PROMPT", "test prompt")
os.environ.setdefault("FEEDBACK_REPO_URL", "https://example.invalid/repo.git")
os.environ.setdefault("STATE_DIR", TMPDIR)
os.environ.setdefault("HOME", TMPDIR)

# Stubs for the two runtime-only clients. Tests never touch the cluster or
# RCON; they exercise the stream parser and the formatting helpers.
if "kubernetes" not in sys.modules:
    k8s = types.ModuleType("kubernetes")
    k8s.client = types.ModuleType("kubernetes.client")
    k8s.config = types.ModuleType("kubernetes.config")
    rest = types.ModuleType("kubernetes.client.rest")

    class ApiException(Exception):
        pass

    rest.ApiException = ApiException
    k8s.client.rest = rest
    sys.modules["kubernetes"] = k8s
    sys.modules["kubernetes.client"] = k8s.client
    sys.modules["kubernetes.client.rest"] = rest
    sys.modules["kubernetes.config"] = k8s.config

if "mctools" not in sys.modules:
    mctools = types.ModuleType("mctools")

    class RCONClient:  # pragma: no cover - never instantiated in tests
        def __init__(self, *a, **k):
            raise RuntimeError("RCON is not available in tests")

    mctools.RCONClient = RCONClient
    sys.modules["mctools"] = mctools

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
