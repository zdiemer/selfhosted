# GPU render device allocation

Run `./upgrade.sh` before deploying Jellyfin or RomM Webstation. A digest-pinned
[squat/generic-device-plugin](https://github.com/squat/generic-device-plugin)
advertises `devic.es/render` on the existing `media.zachd/vaapi=true` nodes. Each
application requests one shared render slot. Only `/dev/dri/renderD128` is supplied
to it, without privileged mode or a host `/dev/dri` directory mount.

The plugin has no Kubernetes API token, no capabilities, a read-only root
filesystem, and a read-only device directory. It can write kubelet's device-plugin
socket directory, which is necessarily a trusted node-management interface.
It exposes no network listener outside loopback.

The eight slots per node share a GPU; this is device access control, not GPU memory
or performance isolation. Jellyfin's hardware H.264 encoding was verified after
removing privileged mode. Steam uses the separate `infra/steam-input-runtime` chart for its Xorg and
dynamic input devices, also without privileged mode.
