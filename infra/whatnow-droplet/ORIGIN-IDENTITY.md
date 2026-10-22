# Stronger origin identity for Whatnow

Assessment 2026-10-22: **not enabled**. We can implement and deploy the host-side
configuration from this workspace, but cannot complete the Cloudflare side with
the credentials currently available. Root SSH and 1Password access do not grant
Cloudflare account administration. Existing tunnel connector tokens do not grant
permission to create a new tunnel, change DNS or configure origin certificates.

## Proposed approach: a zone-specific client certificate

Use a dedicated client certificate for zone-level Authenticated Origin Pulls
(AOP), with Caddy trusting only its dedicated issuing CA. This keeps the existing
Caddy/Compose topology and Cloudflare CIDR firewall. It authenticates possession
of the credential configured for this zone, subject to private-key security.

Cloudflare's **global/shared AOP certificate** only identifies its network, not
this account or zone. That does not satisfy the stronger zone identity objective.
Cloudflare documents the distinction and custom certificate setup in its
[zone-level AOP guide](https://developers.cloudflare.com/ssl/origin-configuration/authenticated-origin-pull/set-up/zone-level/).
An origin server certificate/Full (strict) authenticates the server to Cloudflare;
it is not a substitute for client authentication in the other direction.

Required one-time access: a token scoped to `whatnow.gg` with Zone Read and
**SSL and Certificates Edit/Write**, stored in 1Password. Cloudflare requires that
write permission for both [certificate upload](https://developers.cloudflare.com/api/resources/origin_tls_client_auth/methods/create/)
and [zone enablement](https://developers.cloudflare.com/api/resources/origin_tls_client_auth/subresources/settings/methods/update/).
Check actual zone availability and existing configuration through the API before
committing to the rollout; account entitlement has not been verified here.

No DO or Tailscale API access is required for this approach. Once the Cloudflare
reference is supplied, the rest can be handled from this workspace. A manual
Cloudflare setup by the owner is an alternative, but uploading the custom
certificate and enabling it must still happen before origin enforcement.

## Implementation and rollout

1. Inventory all proxied names and origin settings in `whatnow.gg`, including
   `www`, Full (strict), existing AOP configuration and certificate expiry. Zone
   enablement affects all proxied traffic in the zone; account for other origins.
2. Generate a dedicated CA and client certificate with client-authentication
   usage, bounded lifetime, documented renewal and expiration alerts. Store private
   material in 1Password; stage it only in private memory-backed files. The origin
   receives the public CA certificate, not the client or CA private key.
3. Extend the host Ansible/Compose configuration to mount that CA read-only and
   configure Caddy client authentication for both `whatnow.gg` and `www.whatnow.gg`.
   Validate using the pinned Caddy image. Stage trust without requiring certificates
   yet; verify the existing production path remains healthy.
4. Upload the client certificate/key and enable custom AOP at Cloudflare. Verify
   the expected client certificate actually arrives at Caddy before enforcing it.
   Do not infer success solely from an API response or an ordinary HTTP 200.
5. Require verified client certificates at both HTTPS hosts. Keep the CIDR filter.
   HTTP must serve only the intended redirects and ACME challenge path, never
   application content that bypasses mTLS. Test certificate renewal as well as
   requests: changing origin TLS must not silently break Caddy's ACME behavior.
6. From an isolated/local test path, prove missing and unrelated client
   certificates are rejected and the intended certificate succeeds. Keep test
   private keys out of command arguments, logs and Git. From outside, verify
   proxied health, `www` redirects and blocked direct-origin access. Confirm an
   HTTP request cannot reach application content without authenticated TLS.
7. Reapply for convergence, record evidence and commit/push. Add rotation handling
   with overlap of old/new trust, verify the new certificate, then retire the old
   one. Alert before expiration; do not create an unmonitored outage deadline.

Caddy supports this through its [TLS client authentication configuration](https://caddyserver.com/docs/caddyfile/directives/tls).
This is a rollout plan, not an already-tested Caddy configuration. The provider
IaC added previously owns DNS only: AOP resources and secret handling still need
implementation, including explicit plan-guard support if added to OpenTofu.
Never just broaden the existing guard to accept arbitrary resource creation.

Rollback: restore the previous Caddy client-auth enforcement setting first while
retaining CIDR restrictions; verify health before disabling/changing Cloudflare's
client certificate. Do not disable Cloudflare client authentication first while
Caddy still requires it. This temporarily returns to the present CIDR-only posture.

## Alternative: dedicated Cloudflare Tunnel

A dedicated Whatnow tunnel would use outbound connections and let us close public
origin web ports after a verified DNS cutover. It requires Cloudflare account
Tunnel Edit and zone DNS Edit permissions, or an owner-created/configured dedicated
tunnel whose connector token is placed in the vault. See Cloudflare's
[API setup guide](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel-api/).

Use a separate tunnel identity for this droplet. Do not reuse the home cluster's
connector token: that can join the same tunnel and receive its traffic. Verify
resource limits, client IP handling, health, redirects, local origin isolation,
restart recovery and DNS rollback before closing ports. This is a larger topology
change than custom AOP and still requires Cloudflare-side access.
