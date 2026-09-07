"""Catalog assembly.

k8s.py mostly talks to the API server, which these tests do not; what is
covered here is the pure merge logic that decides which configs exist at all.
"""

from __future__ import annotations

from vmlab import k8s


def test_duplicate_seed_slugs_are_marked_broken_not_rendered_twice(monkeypatch):
    """A slug is the identity of a PVC and of a cache filename, so two seed
    entries sharing one are two tiles fighting over one machine. This went
    unnoticed once — a second `freedos` was added and simply rendered twice."""
    seed = [
        {"slug": "dos", "name": "One", "network": "none"},
        {"slug": "dos", "name": "Two", "network": "none"},
    ]
    monkeypatch.setattr(k8s, "_seed_configs", lambda: seed)
    monkeypatch.setattr(k8s, "_user_configs", lambda: [])
    configs = k8s.list_configs()
    assert len(configs) == 2
    assert configs[0].get("broken") is None
    assert "duplicate slug" in configs[1]["broken"]
    # Distinct slugs, so the UI cannot key two tiles the same.
    assert configs[0]["slug"] != configs[1]["slug"]
