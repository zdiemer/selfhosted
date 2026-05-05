# RomM — self-hosted ROM manager

Helm chart that runs [RomM](https://github.com/rommapp/romm) on our k3s
cluster with the ROM library mounted **read-only over SMB** from the
existing NAS share (the same one the Steam Deck uses). Friends browse and
play in a browser via EmulatorJS; saves and states are per-user and live
on a local PVC inside the cluster.

v1 ships LAN-only with RomM's built-in auth. DuckDNS exposure and Authelia
OIDC are tracked as separate follow-ups below.

## Architecture

```
Windows NAS  ──SMB─→  csi-driver-smb  ──RO─→  romm pod  ──HTTP :8080─→  Service (ClusterIP)
                                                 │
                                                 └── RWO PVC (SQLite + resources + saves)
```

Single Deployment, `Recreate` strategy (one pod at a time — SQLite + single
RWO PVC).

---

## Prerequisites (one-time, manual)

### 1. Install the SMB CSI driver (cluster-wide)

Lets any namespace mount an SMB share as a PV. Install once, reuse forever.

```bash
helm repo add csi-driver-smb \
  https://raw.githubusercontent.com/kubernetes-csi/csi-driver-smb/master/charts
helm repo update
helm upgrade --install csi-driver-smb csi-driver-smb/csi-driver-smb \
  --namespace kube-system \
  --version v1.15.0
```

Verify:

```bash
kubectl -n kube-system get pods -l app=csi-smb-node
kubectl -n kube-system get pods -l app=csi-smb-controller
```

### 2. Create a dedicated SMB user on the NAS

Don't reuse your main Windows account or the Steam Deck user. Make a
fresh one with read-only access to the ROMs share.

On the Windows host serving the share:

1. **Settings → Accounts → Family & other users → Add account** → pick
   "I don't have this person's sign-in info" → "Add a user without a
   Microsoft account". Username like `roms-reader`, long random password.
2. **Computer Management → Local Users and Groups → Users → roms-reader
   → Properties → Member Of** — leave only `Users` (no admin).
3. On the shared folder: **right-click → Properties → Sharing → Advanced
   Sharing → Permissions** — grant `roms-reader` **Read** only.
4. Also under **Security** tab, give `roms-reader` **Read & execute**.
5. Confirm the network profile is **Private** (Settings → Network →
   properties), or the firewall will silently drop SMB traffic from
   the cluster node.
6. From another machine, test with
   `smbclient -U roms-reader //NAS-IP/Games/ROMs` to confirm the creds
   work before touching k8s.

### 3. Create the namespace

```bash
kubectl create namespace games
```

---

## First install

```bash
# 1. Populate values.local.yaml
cp games/romm/values.local.yaml.example games/romm/values.local.yaml
# Edit games/romm/values.local.yaml:
#   smb.host       = NAS IP
#   smb.share      = share path (e.g. Games/ROMs)
#   smb.username   = roms-reader
#   smb.password   = (paste)
#   romm.authSecretKey = $(openssl rand -hex 32)

# 2. Install
helm install romm ./games/romm -n games \
  -f games/romm/values.yaml \
  -f games/romm/values.local.yaml

# 3. Watch the pod come up
kubectl -n games get pods -w
```

First boot takes a minute or two while RomM scans the library.

### Accessing it (LAN-only, v1)

No Ingress until the DuckDNS follow-up. Use `kubectl port-forward`:

```bash
kubectl -n games port-forward svc/romm 8080:8080
# Open http://localhost:8080
```

For access from other LAN devices, either:
- Port-forward with `--address 0.0.0.0` from a gateway box, or
- Temporarily flip the Service to `NodePort` (`service.type: NodePort`)
  and `helm upgrade`. Reach it at `http://<node-ip>:<nodePort>`.

### Create the first admin user

RomM bootstraps with no users. On first visit, it prompts you to create
an **admin** account. Do this before exposing it beyond localhost — the
form is not protected.

Create additional friend accounts from **Admin → Users → Add**. For now,
give each person their own login with the **Viewer** or **Editor** role.

---

## Verification

```bash
# 1. Pod Running
kubectl -n games get pods
#   NAME                    READY   STATUS
#   romm-<hash>             1/1     Running

# 2. SMB mount actually landed
kubectl -n games exec deploy/romm -- ls /romm/library/roms | head
# Should list your per-platform folders (nes/, snes/, psx/, ...).

# 3. RomM finished its scan
kubectl -n games logs deploy/romm | grep -i "scan"

# 4. Browse http://localhost:8080, pick a game, hit Play.
#    EmulatorJS loads in-browser, save states go to the data PVC.
```

---

## Upgrade

```bash
./games/romm/upgrade.sh
```

Wraps `helm upgrade` + rollout wait. Safe to run after bumping
`image.tag` or editing `values.local.yaml`. The `checksum/env` annotation
on the Deployment picks up Secret changes and restarts the pod.

---

## Uninstall

```bash
helm uninstall romm -n games
# PVCs stay so reinstall keeps users/saves/metadata. To nuke:
kubectl -n games delete pvc romm-data
# The SMB-backed PV is Retain; delete it too if you're done:
kubectl delete pv romm-library
```

---

## Follow-up A: Ingress at romm.zachd.duckdns.org

Until this is done, RomM is LAN-only via port-forward.

### How DNS + TLS already work in this cluster

DuckDNS resolves any `*.zachd.duckdns.org` query to the same A record as
the parent domain, so `romm.zachd.duckdns.org` already resolves today —
no DuckDNS account changes and no edits to talaria's `duckdns-updater`
CronJob (which only refreshes the `zachd` record). See
`../../minecraft/bluemap-ingress.yaml` for the same pattern in action at
`map.zachd.duckdns.org`.

For TLS, talaria's Traefik holds a wildcard `*.zachd.duckdns.org` cert
issued via DNS-01 (the only ACME challenge DuckDNS supports — it can
only write TXT at the account's top-level subdomain, so per-host
sub-subdomain certs fail propagation). Every Ingress in this cluster
requests that same wildcard SAN via the `router.tls.domains.0.main` +
`sans` annotations, so new hosts under `*.zachd.duckdns.org` serve from
the cached cert without a second ACME round-trip. The chart's
`templates/ingress.yaml` already does this.

### Steps

1. **Flip RomM's Ingress on.** In `games/romm/values.local.yaml`:

   ```yaml
   ingress:
     enabled: true
     host: romm.zachd.duckdns.org
   ```

   Re-run `./games/romm/upgrade.sh`. Traefik picks up the new Ingress,
   matches it against the cached wildcard, and starts serving. If the
   wildcard hasn't been issued yet, the first request stalls ~30–60s
   while DNS-01 completes; watch
   `kubectl -n kube-system logs deploy/traefik | grep -i acme`.

2. **LAN access without exposing to WAN yet.** If your router is
   already forwarding 443 for talaria, the new subdomain will answer on
   the public internet the instant the Ingress comes up. If you want to
   keep it LAN-only while still getting a real cert, either:
   - Add a Traefik IP-allowlist middleware restricting to your LAN
     CIDR, referenced from the Ingress annotations, **or**
   - Keep `ingress.enabled: false` and use split-horizon DNS (hosts
     file or AdGuard Home rewrite) mapping `romm.zachd.duckdns.org` to
     the node's LAN IP. No cert needed if you stick to HTTP.

---

## Follow-up B: Authelia OIDC (shared auth for all services)

See `../../auth/authelia/README.md` for standing up Authelia itself.
Once Authelia is running at `https://auth.zachd.duckdns.org` (real TLS,
not self-signed — RomM's OIDC client rejects HTTP issuers), wire RomM:

1. **Register RomM as an OIDC client in Authelia.** In the Authelia
   chart's `values.local.yaml`, add under `oidc.clients`:

   ```yaml
   - id: romm
     description: RomM
     secret: "$pbkdf2-sha512$..."   # hash with: authelia crypto hash generate pbkdf2 --variant sha512
     public: false
     redirect_uris:
       - https://romm.zachd.duckdns.org/api/oauth/openid
     scopes: [openid, profile, email, groups]
     userinfo_signing_algorithm: none
   ```

   Store the plaintext client secret (the one you'll give RomM) in the
   RomM `values.local.yaml` — only the hashed form lives in Authelia's
   config.

2. **Enable OIDC in RomM.** In `games/romm/values.local.yaml`:

   ```yaml
   romm:
     oidc:
       enabled: true
       serverName: "Authelia"
       issuer: "https://auth.zachd.duckdns.org"
       clientId: "romm"
       clientSecret: "<plaintext, matching Authelia's hash>"
   ```

   `./games/romm/upgrade.sh`. Sign-in page should now show an
   "Authelia" button alongside the built-in form.

3. **Friend onboarding** becomes "add a user in Authelia", not in RomM.
   RomM will lazily create a local user row the first time each person
   signs in via OIDC.

---

## Directory layout inside the container

```
/romm/
  library/
    roms/<platform>/<file>        ← SMB mount, read-only
    bios/<platform>/<file>        ← (optional, not mounted by default)
  resources/                      ← metadata, covers                 (data PVC)
  assets/                         ← saves, states, screenshots       (data PVC)
  config/                         ← SQLite db, app config            (data PVC)
```

If your SMB share's root is already laid out as `roms/` + `bios/` folders
(not just per-platform dirs), change `smb.mountPath` to `/romm/library`
and (if needed) `smb.sharePath` to select a subfolder of the share.

---

## Upstream

- RomM: <https://github.com/rommapp/romm>
- Docs: <https://docs.romm.app>
- EmulatorJS: <https://emulatorjs.org>
- SMB CSI driver: <https://github.com/kubernetes-csi/csi-driver-smb>
