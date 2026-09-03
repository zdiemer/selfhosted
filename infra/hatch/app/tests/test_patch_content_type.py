"""Regression test for a silent, expensive trap.

The generated kubernetes client negotiates a patch Content-Type with
select_header_content_type([json-patch, merge-patch, strategic-merge,
apply-patch]), which returns content_types[0] when "application/json" is absent
— so patches default to application/json-patch+json, which rejects a dict body.
`_content_type=` is not an accepted kwarg and raises ApiTypeError.

Worse than the crash is the near miss: application/merge-patch+json WOULD be
accepted, and would replace the whole pod-template annotations map, deleting the
target chart's own checksum/* annotations and rolling it for unrelated reasons.

This test asserts the mechanism that prevents both, without touching a cluster.
"""

from __future__ import annotations

from kubernetes import client


def test_default_header_wins_over_negotiated_content_type():
    """ApiClient.__call_api applies default_headers AFTER the negotiated value,
    which is the only reason set_default_header works here."""
    import inspect

    src = inspect.getsource(client.ApiClient._ApiClient__call_api)
    negotiated = src.index("header_params = header_params or {}")
    defaults = src.index("header_params.update(self.default_headers)")
    assert defaults > negotiated, "default_headers no longer override; patches would go out as json-patch"


def test_client_would_otherwise_pick_json_patch():
    """If this ever starts returning strategic-merge on its own, the dedicated
    patch client can go away — until then it is load-bearing."""
    api = client.ApiClient()
    picked = api.select_header_content_type([
        "application/json-patch+json",
        "application/merge-patch+json",
        "application/strategic-merge-patch+json",
        "application/apply-patch+yaml",
    ])
    assert picked == "application/json-patch+json"


def test_patch_method_rejects_content_type_kwarg():
    """The kwarg that looks like it should work, and does not."""
    import inspect
    import re

    src = inspect.getsource(client.AppsV1Api.patch_namespaced_deployment_with_http_info)
    all_params = re.search(r"all_params = \[(.*?)\]", src, re.S).group(1)
    assert "_content_type" not in all_params
