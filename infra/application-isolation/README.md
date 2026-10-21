# Application network isolation

`./upgrade.sh` manages outbound NetworkPolicies for the applications listed in
`values.yaml`. They can reach public IPv4 destinations, cluster DNS, services in
their namespace, and the authenticated egress proxy. LAN, tailnet, metadata,
loopback destination ranges and multicast destinations are excluded. Reply
traffic and node-managed storage mounts are unaffected. No IPv6 egress is granted.

Tika and Gotenberg retain their stricter no-egress policies. Backup/operator jobs
are not selected. New applications must be added explicitly. Add narrow
cross-namespace dependencies here when an application needs them; do not restore
a broad private-network allowance.

This is application segmentation, not isolation from the kernel or hosting node.
Kubernetes does not isolate a pod from its own node with ordinary NetworkPolicy,
and Steam has a dedicated policy in its own chart. Services within a namespace are still mutually
reachable unless their own ingress policies deny it. Other additive policies can
grant additional access, so review the effective policy when adding one.

Verified from Gamedex: NAS HTTPS/NFS, remote-node Kubernetes/Headlamp, the Steam
node listener, tailnet parent and metadata are blocked; public HTTPS and RomM
remain available. Paperless retains access to its parsers and public HTTPS.
