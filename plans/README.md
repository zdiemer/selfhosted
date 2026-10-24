# plans

Designs for work that isn't done yet, one Markdown file per plan.

Everything else in this repo documents something that *runs* — a chart, its
values, the reasoning behind its current shape. This folder is the other half:
changes that are decided but not deployed, usually because they're blocked on
hardware, on a purchase, or on a maintenance window.

A plan earns a file here when the reasoning behind it is worth more than the
diff that eventually implements it — the constraints that were measured, the
approaches that were ruled out and why, the order the steps have to happen in.
That context is expensive to rebuild and easy to lose between the conversation
that produced it and the evening months later when there's time to do the work.

When a plan is executed, the durable parts move into the READMEs of whatever it
touched and the file is deleted. A plan left here after its work has shipped is
a stale plan, and will be read as a live one.

| Plan | What it covers | Status |
|---|---|---|
| [`network-segmentation.md`](network-segmentation.md) | Splitting the flat `192.168.4.0/22` home LAN into cluster / trusted / untrusted VLANs, with the NAS dual-homed by direct IP on two of them and the SMS relay handset moved to wired isolation. Requires replacing the eero's routing and the unmanaged switch. | Not started — blocked on hardware |
| [`runner-cutover-vxp-zodemu.md`](runner-cutover-vxp-zodemu.md) | Finishing the move of vxp and zodemu onto self-hosted runners: adding `vxp` to the fine-grained PAT, creating the `arc-vxp` scale set, and reprovisioning the win11 guest so it runs one agent per repo with MSVC / rustup / .NET installed. All three repos' workflows are already pointed at it. | Code shipped 2026-09-11 — blocked on the GitHub PAT UI and an RDP session |
| [`automated-cluster-agent.md`](automated-cluster-agent.md) | Bounded cluster repairs, independent verification, automatic tested merges/deployments, and escalation policy. | Planned — implementation not started |
| [`provider-iac-adoption.md`](provider-iac-adoption.md) | Complete live adoption of DigitalOcean, Cloudflare DNS, and Tailscale resources with the prepared OpenTofu tooling. | Code prepared — blocked on provider credentials and live inventory/import |
| [`whatnow-origin-identity.md`](whatnow-origin-identity.md) | Add zone-specific authenticated origin pulls for Whatnow, with a dedicated tunnel as an alternative. | Planned — blocked on Cloudflare SSL API access |
| [`laundry-completion.md`](laundry-completion.md) | Commission sensors, calibrate real appliance cycles and verify received notifications. | Backend deployed — sensor not yet wired |
| [`keepass-completion.md`](keepass-completion.md) | Finish vault/client adoption, conflict testing and recovery validation. | Services deployed — owner workflow unfinished |
| [`steam-moonlight-completion.md`](steam-moonlight-completion.md) | Complete Steam login, Moonlight pairing and real game/input/persistence validation. | Infrastructure deployed — real client session unverified |
| [`legacy-mmo-completion.md`](legacy-mmo-completion.md) | Finish FFXIV 1.0 and SMT IMAGINE client testing and set up both games through the private `romnas` repo. | Testing unfinished — Romnas setup pending |
| [`ios-device-gateway.md`](ios-device-gateway.md) | Dedicated iOS device for iMessage gateway and low-latency browser gaming, with app/file management and battery monitoring. | Design agreed — hardware selection and same-device feasibility tests first |
| [`mac-mini-ci.md`](mac-mini-ci.md) | Used 16 GB Apple Silicon Mini for private macOS CI, development VM, Rosetta compatibility tests and tailnet browser access. | Planned — hardware purchase and access/VM validation pending |
| [`distributed-node-storage.md`](distributed-node-storage.md) | Unequal node-disk inventory, parity storage shortlist and migration feasibility gates; preserve NAS. | Inventoried — two-host protection; full redundancy may wait for repair; provider and sizing pending |

## Other open work

- [Droplet recovery rehearsal](../infra/droplet-recovery/README.md): procedure
  prepared, actual provider-backup restore and clean-host rebuild untested.
  Explicitly deferred by the owner; do not silently reactivate it.
- Laptop-6 stability: cordoned for an authorized one-boot comparison on
  `6.17.0-22-generic`; normal GRUB default remains `6.17.0-29-generic`.
  Remote access and K3s returned successfully, but boot success under light
  infrastructure load does not establish that application crashes are fixed.
  Next step is a bounded reproduction of the affected workloads before deciding
  whether to uncordon or retain this kernel. BIOS remains unchanged and the
  crash cause is unproven. No permanent kernel downgrade is selected.

Offsite cluster backups were explicitly declined on cost grounds; they are not
an unfinished implementation to restart. Application feature backlogs in sibling
repositories remain in those repositories rather than being duplicated here.
