# Pod Security admission

`policy.json` owns the namespace labels for Kubernetes Pod Security Admission.
The standard version is pinned to **v1.35**, matching the cluster, so an upgrade
does not silently introduce new admission requirements.

```sh
python3 infra/pod-security/apply.py          # preflight only
python3 infra/pod-security/apply.py --apply  # preflight, label, verify denials
```

The default is **baseline**. CDI, FSU and KEDA enforce **restricted**. All managed
namespaces warn and audit against restricted, including explicit privileged
exceptions. Existing workloads are not restarted or evicted by this operation.
The apply does not create pods: all pod submissions use server-side dry run.

Before applying any labels, the script checks running/pending pods, Deployment,
StatefulSet, DaemonSet, Job and CronJob templates, GitHub runner templates and
K8up PreBackupPods against the intended level in temporary namespaces. These
namespaces are removed in `finally`. The check reconstructs StatefulSet PVC
references, normalizes service-account identity for the scratch namespace, and
removes already-expanded scheduling selectors; it preserves security contexts,
volumes, host namespaces and ports. It does not prove every possible future
operator-generated pod is compatible.

Dormant runner namespaces and VM Lab deliberately stay baseline: empty runner
namespaces are not proof of restricted compatibility, and some VM Lab network
profiles require SETUID/SETGID. Backup jobs commonly run as root, so application
namespaces are not promoted solely because their current web pods are non-root.
After labeling, a privileged pod must be rejected in every enforcing namespace;
restricted namespaces must also reject a pod missing restricted security fields.

## Exceptions and limitations

The nine namespace exceptions and their reasons are explicit in `policy.json`.
They cover storage/device/virtualization agents and a few application-specific
requirements (Steam host ports, Gluetun NET_ADMIN, Minecraft hostPath, Talaria
IPC_LOCK and restore capabilities). **PSA privileged is not a security boundary**:
other workloads in those namespaces inherit the exemption. Moving privileged
components into dedicated namespaces or replacing the exceptional features is
follow-up work. This change preserves Jellyfin's writable media mount.

The script labels existing namespaces; it is not an API-server default or an
admission policy for future Namespace objects. Run it after adding namespaces
and before deploying their workloads. It refuses to silently weaken an existing
stronger enforce label. Namespace-label write access must remain administrative.
Audit labels only produce useful retained records if API audit logging collects
them; this change does not configure an audit-log backend.

Recovery: use the previous namespace labels to restore a prior admission level
if an unmodeled generated workload is rejected, then codify the correction in
`policy.json`. Do not disable enforcement cluster-wide for one incompatible pod.

See [Kubernetes namespace enforcement](https://kubernetes.io/docs/tasks/configure-pod-container/enforce-standards-namespace-labels/).
