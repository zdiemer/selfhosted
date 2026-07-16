#!/usr/bin/env bash
# Mint a login token for the Headlamp UI.
#
# This prints a cluster-admin credential. It is short-lived (the API server's
# default, ~1h) but while valid it is full control of the cluster. Don't paste it
# anywhere it might be logged or shared.

set -euo pipefail

RELEASE="${RELEASE:-headlamp}"
NAMESPACE="${NAMESPACE:-headlamp}"

exec kubectl create token "$RELEASE" -n "$NAMESPACE" "$@"
