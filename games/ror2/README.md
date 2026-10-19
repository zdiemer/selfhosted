# Risk of Rain 2 unofficial dedicated server

The experimental [server image and Helm chart](https://github.com/zdiemer/ror2-unofficial-dedicated-server-docker)
run the Windows game through Proton with the companion BepInEx plugin. The
image contains no game assets. The pod mounts the existing `romm-library` SMB
claim read-only at `Roms/_steam/Risk of Rain 2`, stages the 2.8 GiB install on
the work claim, and starts one server on UDP 7777.

`./upgrade.sh` deploys the chart from `~/Code/ror2-unofficial-dedicated-server-docker`.
Build and push the image referenced in `values.yaml` before changing its tag.
The 16 GiB `truenas-iscsi` work claim keeps the writable game copy and Proton
prefix across pod restarts. Bump `game.revision` in `values.yaml` after
replacing the NAS install to refresh that copy. The claim is excluded from
backups because the original game files remain on the NAS.

The `LoadBalancer` service publishes UDP 7777 on all cluster nodes, including
the tailnet node pointed to by `*.zachd.duckdns.org`. The client must be on
the tailnet. At the game's main menu, press `Ctrl` + `Alt` + backtick (the key
below Esc), then enter:

```text
connect "rain.zachd.duckdns.org:7777"
```

The plugin's direct-IP path does not check Steam tickets; keep this on the
trusted tailnet during testing. Check startup with
`kubectl -n games logs deployment/ror2-ror2 -f` and check the service with
`kubectl -n games get service ror2-ror2`.

The 0.1.1-1 image explicitly budgets Unity updates and rendering because this
Proton build still initializes llvmpipe despite `-nographics`. Empty servers
use 10 update FPS, connecting/connected clients use 60, and initialized servers
render once per second. The 2026-10-19 rollout reached Ready with UDP 7777 bound,
zero players/connections, and approximately 0.10 CPU cores over a 20-second
measurement (previously 4 cores). A real-client join/run after this change still
needs verification. Startup logs are cleared so readiness cannot match an old run.
