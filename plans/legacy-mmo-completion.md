# Finish FFXIV 1.0 and SMT IMAGINE testing and Romnas setup

Status: Server infrastructure documented; finish real client testing and set up
both games through the private `romnas` repository.

## Remaining work

- [ ] Finish testing [FFXIV 1.0 / 1.23b](../games/ffxiv-1x/README.md) with a
  real client: installation and patching, account login, character creation,
  zone loading, movement and the available opening content. Record failures
  separately from the upstream limitations in
  [`CONTENT.md`](../games/ffxiv-1x/CONTENT.md).
- [ ] Finish testing [SMT IMAGINE](../games/smt-imagine/README.md) with a real
  client: matching client/server versions, staged client data, account login,
  character creation, zone loading and a playable session with combat and demon
  interaction.
- [ ] For both games, verify reconnecting and persistence of characters and
  progress after a planned server restart, within each emulator's supported
  behavior. Confirm access from the intended tailnet client machines.
- [ ] Set up both games through the private `romnas` repo. Inspect its existing
  installation and launch conventions, then add the client setup, patching,
  server configuration and launch support needed for each game there.
- [ ] Validate each Romnas setup from a fresh client installation through login
  and gameplay against these servers. Keep client assets and account credentials
  out of this repository; document their private storage/configuration in Romnas.

Done when both games have completed real client testing and can be installed,
configured and launched through Romnas. Keep Romnas implementation details in
that repository; move verified server instructions and remaining upstream
limitations into the service READMEs, then remove this plan.
