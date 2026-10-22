# Application network isolation

`./upgrade.sh` manages outbound NetworkPolicies for the named applications in
`values.yaml`. Each application can reach its own pods, public IPv4 destinations,
cluster DNS, the authenticated egress proxy, and its explicit dependencies.
Sharing a namespace no longer grants connectivity to unrelated applications.
LAN, tailnet, metadata, loopback destination ranges and multicast are excluded.
Reply traffic and node-managed storage mounts are unaffected. No IPv6 egress is
granted. Jellyfin's writable media mount is unchanged.

Dependencies include Gamedex/RomM, RomM/Webstation, Seerr/Arr/Jellyfin,
Arr/Bitmagnet, Authelia/Redis, and Talaria/SMS-relay/Ollama/FlareSolverr. Talaria
reaches only Arr's FlareSolverr component on TCP 8191 for Mercari scraping and
listing refreshes. Talaria's diagnostic
workers additionally reach the Kubernetes API on the three declared servers;
update those endpoint addresses when replacing control-plane nodes. RBAC limits
those service accounts to Talaria's namespace. Ollama's ingress still selects
only the configured inference workers.

Tika and Gotenberg retain their stricter no-egress policies. RomM, Bitmagnet,
Vocard and Rachel databases accept only their app's clients and K8up dump pods
on the database port. Backup/operator jobs are not selected by application
egress policies. New apps and dependencies must be added explicitly.

This does not isolate a pod from its hosting node/kernel. Steam has a dedicated
policy in its own chart. Components of one application still share access, and
other additive policies can grant more access. Infrastructure/operator workloads
need their own dependency review; they are not covered by this application list.

Validated after deployment: Gamedex reaches RomM/public HTTPS but cannot reach
RomM's database, RA2, NAS HTTPS or metadata. Talaria reaches its database/Redis,
SMS relay, Ollama and the Kubernetes API while NAS and metadata are blocked.
Seerr reaches Jellyfin/Sonarr/Radarr but cannot reach Bitmagnet's database.
Talaria and Authelia public HTTP paths remain healthy.
