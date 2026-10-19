# KEDA

The existing `keda` Helm release uses upstream defaults. `upgrade.sh` pins the
chart and application to 2.20.0, whose tested Kubernetes range is 1.33–1.35:
https://keda.sh/docs/2.20/operate/cluster/

Run this separately before the K3s 1.35 rollout. The chart manages its CRDs
and supplies the `events.k8s.io` RBAC required by 2.20. Talaria's Redis
ScaledObjects stay in the Talaria repository. Their bounds, cooldowns and HPA
scale-down policies must not change during this upgrade.

Verify all three KEDA deployments, the external metrics APIService, and every
ScaledObject's Ready condition. A future Kubernetes 1.36 rollout needs another
compatibility review; 2.20's published matrix stops at 1.35.
