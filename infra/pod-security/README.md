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
restore capabilities). **PSA privileged is not a security boundary**:
PSA alone leaves other workloads in those namespaces exempt. The application
guards below now limit that exposure in four namespaces. Moving privileged
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

## Application exception guardrails

```sh
python3 infra/pod-security/guards.py          # temporary policies, server dry runs
python3 infra/pod-security/guards.py --apply  # repeat preflight, enforce and verify
```

Native ValidatingAdmissionPolicies now constrain the `games`, `media`,
`minecraft` and `talaria` PSA exceptions. There is no webhook service or new
privileged agent. Policy errors fail closed; the installer checks CEL type
warnings and live controller/pod templates before applying production bindings.
Both pod creation/update and ephemeral-container updates are covered.

All four forbid privileged containers, host PID/IPC/network namespaces,
arbitrary hostPath mounts and host ports, non-baseline capabilities, unconfined
seccomp/AppArmor, unmasked proc mounts, Windows HostProcess and pod sysctl
changes. The allowed deviations are:

- Steam's declared TCP/UDP streaming ports, only on its named container and
  application label. This does not restore a privileged pod.
- `NET_ADMIN` on the Gluetun container in the labelled Arr qBittorrent workload.
- The bridge's exact `/home/zachd/.claude` Directory mount under its application
  label and service account. Its shared credential directory remains writable
  for token refresh; eliminating that dependency is separate work.
- Terraria's companion uses that same exact directory in `games`, under the
  `terraria` application label and `terraria-claude` service account. Only its
  `claude` container may mount the credential volume, at `/host-claude`.
- `DAC_READ_SEARCH` on the isolated PostgreSQL recovery init container. The
  current restore must finish before retiring its old immutable Job template.

Elasticsearch's unused `IPC_LOCK` capability was removed: memory locking is
explicitly disabled in its configuration. Jellyfin's media write access is
unchanged. No game connection probes are used.

These are targeted guardrails, not a claim of full restricted-profile compliance
or an authentication boundary for pod labels. Anyone who can create pods and
choose service accounts/labels in a namespace can request the allowed exception;
RBAC must still restrict those rights. Namespace splits and removal of remaining
host dependencies can tighten the exceptions further.

The preflight temporarily creates namespaces and policies, submits current
workload templates and eight dangerous pod variants with server-side dry runs,
and cleans up in `finally`. No test pods are run. To roll back a faulty guard,
remove only its `application-guard-NAMESPACE` ValidatingAdmissionPolicyBinding,
correct the source, and rerun preflight; do not disable all namespace admission.
