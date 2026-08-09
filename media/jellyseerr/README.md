# jellyseerr — the request desk

Jellyseerr at `https://requests.zachd.duckdns.org`: browse and request
movies/TV, sign in with your Jellyfin account. Approved requests are handed
to Radarr/Sonarr ([`media/arr`](../arr/)), which acquire and import; the item
then appears in [Jellyfin](../jellyfin/) and Jellyseerr flips it to
"available" and notifies the requester.

Not behind Authelia — it authenticates against Jellyfin itself, so household
members have one account for both, and a forward-auth portal in front would
just be a second login.

## First-run wiring (in the UI, stored in the config PVC)

1. Sign in as the Jellyfin admin → point it at
   `http://jellyfin.media.svc.cluster.local:8096`.
2. Settings → Services → add Radarr:
   `http://arr-radarr.media.svc.cluster.local:7878`, its API key
   (Radarr → Settings → General), root folder `/media/movies`, quality
   profile of your choice, mark as default.
3. Same for Sonarr: `http://arr-sonarr.media.svc.cluster.local:8989`, root
   folder `/media/tv`.
4. Users tab → import Jellyfin users; set auto-approve (or leave requests
   pending for admin approval).

All of that state (plus request history) is SQLite on a 5Gi `truenas-iscsi`
PVC, backed up by k8up like every other small config volume.
