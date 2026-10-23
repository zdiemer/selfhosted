# Operations

Keep reusable procedures and architecture here. Outstanding designs and
implementation checklists live in [plans](../plans/README.md). Deployment
values and scripts own version pins; Git history owns completed changes. Do not
add incident timelines, rollout transcripts, or point-in-time health inventories.
Put service-specific operating guidance beside that service's chart.

| Document | Purpose |
|---|---|
| [Disaster recovery](disaster-recovery.md) | Recovery dependencies, bootstrap order, and backup checks |
| [K3s upgrades](k3s-upgrade.md) | One-node upgrade procedure and failure recovery |
| [Notification routing](notification-routing.md) | Topics, fallback policy, and public webhook configuration |
| [SMS gateway recovery](sms-gateway-recovery.md) | Handset checks and delivery verification |
| [Shared repository instructions](code-tree-CLAUDE.md) | Source for the parent code-tree CLAUDE.md |

[Paperless](paperless-ngx/) and [Stirling PDF](stirling-pdf/) are application
charts in the document-services category. Images and [capture tooling](capture/termshot.py)
support the repository README.

Host package maintenance is documented in [node operations](../scripts/k3s/README.md#apt-reliability).
Backup implementation and restore tooling live in [K8up](../infra/k8up/).
