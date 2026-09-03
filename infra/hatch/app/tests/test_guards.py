"""The deny-lists are the security boundary, so they get the tests.

Run with:  cd infra/hatch/app && python3 -m pytest tests -q
(Requires the app's dependencies; there is no CI job for this — the repo's
lints are chart-level.)
"""

from __future__ import annotations

from hatch_api.guards import _matches


def test_bare_name_matches_any_namespace_and_kind():
    assert _matches("traefik", "deployment", "kube-system", "traefik")
    assert _matches("traefik", "daemonset", "media", "traefik")
    assert not _matches("traefik", "deployment", "kube-system", "traefik-crd")


def test_namespaced_form_requires_both():
    assert _matches("infra/hatch", "deployment", "infra", "hatch")
    assert not _matches("infra/hatch", "deployment", "media", "hatch")


def test_kind_qualified_form():
    assert _matches("Deployment:infra/alloy", "deployment", "infra", "alloy")
    assert not _matches("Deployment:infra/alloy", "daemonset", "infra", "alloy")


def test_globs():
    assert _matches("democratic-csi*", "deployment", "infra", "democratic-csi-node")
    assert _matches("buildkit*", "deployment", "infra", "buildkit")
    assert not _matches("democratic-csi*", "deployment", "infra", "csi-democratic")


def test_glob_in_namespace_position():
    assert _matches("*/alloy", "daemonset", "infra", "alloy")
