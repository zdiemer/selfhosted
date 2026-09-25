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
