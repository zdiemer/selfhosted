#!/usr/bin/env python3
"""Compare intended entrypoint protections with Traefik's deployed arguments."""
import json
import sys

import yaml


def verify(hcc, deployment):
    values = yaml.safe_load(hcc["spec"]["valuesContent"])
    containers = deployment["spec"]["template"]["spec"]["containers"]
    args = next(c["args"] for c in containers if c["name"] == "traefik")
    actual = dict(arg[2:].split("=", 1) for arg in args if "=" in arg)
    for port, settings in values.get("ports", {}).items():
        if "middlewares" in settings or "redirections" in settings:
            raise ValueError(f"{port}: HTTP settings must be nested under http")
        http = settings.get("http", {})
        expected = ",".join(http.get("middlewares", []))
        key = f"entryPoints.{port}.http.middlewares"
        if actual.get(key, "") != expected:
            raise ValueError(f"{port}: expected middleware chain {expected!r}, "
                             f"found {actual.get(key, '')!r}")
        redirect = http.get("redirections", {}).get("entryPoint")
        if redirect:
            key = f"entryPoints.{port}.http.redirections.entryPoint"
            if not actual.get(key + ".to"):
                raise ValueError(f"{port}: HTTPS redirect is missing")
            if actual.get(key + ".scheme") != redirect["scheme"]:
                raise ValueError(f"{port}: redirect scheme does not match")


if __name__ == "__main__":
    try:
        with open(sys.argv[1]) as f:
            hcc = yaml.safe_load(f)
        with open(sys.argv[2]) as f:
            deployment = json.load(f)
        verify(hcc, deployment)
    except (ValueError, KeyError, StopIteration) as exc:
        sys.exit(str(exc))
