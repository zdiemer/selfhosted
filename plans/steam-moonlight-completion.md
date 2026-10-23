# Finish Steam and Moonlight setup

Status: Steam/Sunshine infrastructure deployed; real client/game validation remains unfinished.

## Existing work

The [Steam guide](../games/steam-headless/README.md) documents the node6 GPU,
local library volume, Sunshine, virtual input and tailnet-only access. Its
validation covers AMD rendering, VAAPI encoders, synthetic controller input
and network boundaries, but explicitly excludes an actual Moonlight session
and installed game. The Deployment is currently ready.

## Remaining work

- [ ] Complete the Steam installer and owner login/Steam Guard through the
  protected desktop. Confirm the persistent games volume is the default library.
- [ ] Pair the intended Moonlight clients through Sunshine's PIN flow.
- [ ] Install an owner-selected game and run an actual session: picture, sound,
  keyboard/mouse or controller, input hotplug, and sustained playable performance.
- [ ] Test LAN and intended remote tailnet access using node6's address; keep
  streaming and administration within the documented access boundaries.
- [ ] Verify GPU rendering and hardware encoding during gameplay. Check behavior
  without a physical display; resolve a missing sink if it causes software rendering.
- [ ] Test disconnect/reconnect and a planned pod restart: Steam login, library,
  settings and Sunshine pairing must persist. Check idle resource use afterward.
- [ ] Verify home/config backup coverage; downloaded games remain rebuildable.

Done when the owner can launch and play a game from each intended client and
resume after restart. Move confirmed settings into the service README and
remove this plan; synthetic input tests alone do not satisfy completion.
