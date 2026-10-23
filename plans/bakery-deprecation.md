# Finish Bakery deprecation

Status: harness removal committed; workspace rollout remains pending.

Bakery was an additional agent harness inside the Claude workspace. Commit
`d3eab7d` removed its chart containers and related references. The live
`claude-workspace` Deployment still includes `bakery-server` and `bakery-web`
on image `v43`, so the source cleanup has not completed the operational cutover.

## Remaining work

- [ ] Review the current chart/image diff and identify any active Bakery sessions
  or artifacts that need preserving. Retain its persistent data unless deletion
  is separately requested; retiring containers does not require erasing history.
- [ ] Prepare the normal [workspace deployment](../dev/claude-workspace/README.md).
  Build a new uniquely tagged image if image sources changed; render the release
  with its real secret references without printing their values.
- [ ] Include the shared AGENTS.md bootstrap change in the rollout and verify it
  preserves the existing code-tree instructions for all supported agents.
- [ ] Arrange the workspace restart around active agent work and preserve a
  checkpoint outside the terminating processes. Use the supported upgrade path.
- [ ] Confirm both Bakery containers and their routes/services are absent from
  the live release; review retired overrides without touching unrelated credentials.
- [ ] Verify terminal access, Happy/Codex, journal, Signal/WhatsApp gateway,
  Tailscale and secret resolution still work. Do not send test messages without
  explicit authorization.
- [ ] Reconcile dashboard/monitoring references with the retired services and
  retain a rollback release/image until replacement behavior is verified.

Done when the deployed workspace matches the removal and retained workflows
pass validation. This plan does not authorize deleting Bakery session data.
