# SMB CSI

This directory manages the existing `csi-driver-smb` Helm release in
`kube-system`, pinned to chart/image 1.19.0. Run `./upgrade.sh` here.
Use `python3 verify.py --node zachd-ubuntu-6` for read-only checks of current
static claims. It creates short-lived reader Jobs and removes them afterward.

No share credentials are stored here: workloads retain their existing Secrets
and PVs. The unused Windows DaemonSet is disabled.

The controller and Linux node mount helpers remain privileged and use host
networking. Mount propagation and kernel SMB mounts make these trusted host
components, not application sandboxes. Sidecars drop all capabilities, disable
privilege escalation, use RuntimeDefault seccomp and read-only root filesystems.
The upstream controller metrics listener is patched to `127.0.0.1:29644`;
health listeners already use localhost. The post-renderer fails if the chart
changes the expected workloads or metrics argument. Host firewall rules provide
an additional boundary for these ports.

Validated 2026-10-22: controller and all ten node plugins rolled successfully.
Fresh read-only jobs mounted all four existing shares on a node: Cloud Game,
RomM library and Smitele passed file reads; RomM saves passed directory access
(no file found within the bounded test depth). No production files were written.
All temporary jobs were removed. This does not test dynamic SMB provisioning;
all four current PVs are static.

Upstream: [SMB CSI deployment](https://github.com/kubernetes-csi/csi-driver-smb/tree/v1.19.0/deploy).
