# Shared node prerequisites

Run `python3 infra/node-configuration/apply.py` to review the inventory, then
add `--apply` when enrolling a node or changing its memory. The operator needs
cluster administrator access. No application holds node-write credentials and
no permanent privileged agent is deployed.

The script labels nodes below 6 GiB `infra.zachd.dev/memory-tier=low`, and others
`standard`, for soft scheduling preferences. It persists Elasticsearch's
`vm.max_map_count >= 262144` requirement on every eligible node without reducing
an existing larger value. Host changes use short-lived, token-free maintenance
Jobs, removed after verification. No reboot is required.

This replaces Talaria's node-labeler CronJob and privileged Elasticsearch init
container. Provision these prerequisites before deploying Talaria on new nodes.
Legacy `talaria.io/memory-tier` labels can remain during migration; consumers now
use the shared key. Outage alerts belong to Grafana and the cluster dashboard
belongs to `infra/cluster-status`.
