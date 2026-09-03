from __future__ import annotations

import pytest

from hatch_api.config import _parse_api_keys


def test_object_form_with_scopes():
    keys = _parse_api_keys('{"hatch": {"key": "abc", "scopes": ["read"]}}')
    assert keys[0].name == "hatch"
    assert keys[0].scopes == frozenset({"read"})


def test_bare_string_form_means_both_scopes():
    keys = _parse_api_keys('{"hatch": "abc"}')
    assert keys[0].scopes == frozenset({"read", "act"})


def test_empty_is_no_keys_not_an_error():
    # main.py turns "no keys" into a startup exit; parsing itself is silent.
    assert _parse_api_keys("") == []


def test_unknown_scope_is_rejected():
    with pytest.raises(ValueError, match="unknown scopes"):
        _parse_api_keys('{"hatch": {"key": "abc", "scopes": ["admin"]}}')


def test_empty_key_is_rejected():
    with pytest.raises(ValueError, match="empty key"):
        _parse_api_keys('{"hatch": {"key": ""}}')
