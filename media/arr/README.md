
### Preserve FlareSolverr's resource budget during upgrades

FlareSolverr requires the tracked 1Gi memory request / 4Gi limit for the four
Waitress request threads shared by Prowlarr and Talaria. The October 19 version
upgrade retained stale Helm values (512Mi request / 2Gi limit), undoing the live
memory fix while leaving the correct defaults in `values.yaml`. Four concurrent
challenge requests then OOM-killed the browser container.

Use this directory's `upgrade.sh`, which reapplies the tracked values, rather
than a tag-only `helm upgrade --reuse-values`. If deliberately reusing values,
explicitly carry forward any resource-policy changes too. Verify the deployed
container resources after an upgrade; the repository defaults alone do not
prove the live budget. Restoring 1Gi/4Gi passed four simultaneous test solves
with HTTP 200, roughly 1.6Gi sampled peak memory, and zero OOM events. A simple
page test does not establish the worst-case memory cost of real challenges.
