# Vocard — self-hosted Discord music bot

A Helm chart that runs [Vocard](https://github.com/ChocoMeow/Vocard) plus its
required backends (Lavalink + MongoDB + Spotify tokener) on our k3s cluster.

The bot runs as a Discord gateway client — outbound WebSocket to Discord,
no inbound ports, no public endpoint. Friends queue songs in voice channels
with slash commands (`/play`, `/skip`, `/queue`, `/pause`, etc.).

## Architecture

Four pods in the `discord` namespace, all `ClusterIP`:

```
Discord gateway  ← WebSocket ─── vocard-bot  ──── vocard-mongo (queue state / settings)
                                     │
                                     └─── HTTP (LavaLink protocol) ──→ vocard-lavalink
                                                                        │
                                                                        └─── vocard-spotify-tokener
                                                                             (anonymous Spotify tokens)
```

The bot mounts `settings.json` from a Kubernetes Secret that the chart
renders from `values.yaml` + `values.local.yaml`. Rotating the Discord
token is a one-line edit in `values.local.yaml` followed by
`./upgrade.sh`.

---

## One-time Discord setup

Do this once per bot you want to run. Takes ~5 minutes.

### 1. Create the application

1. Go to <https://discord.com/developers/applications> → **New Application** → name
   it (e.g. "Vocard — BoyesServer").
2. **General Information** tab → copy the **Application ID**. This is your
   `discordClientId`.
3. **Bot** tab → **Reset Token** → copy immediately (shown exactly once).
   This is your `discordToken`.
4. Still on the **Bot** tab → scroll to **Privileged Gateway Intents** → enable
   all three:
   - Presence Intent
   - Server Members Intent
   - Message Content Intent

### 2. Invite the bot to your server

1. **OAuth2** tab → **URL Generator**.
2. **Scopes** — tick `bot` and `applications.commands`.
3. **Bot Permissions** (appears below Scopes once `bot` is ticked) — tick:
   - View Channels
   - Send Messages
   - Embed Links
   - Read Message History
   - Connect *(voice)*
   - Speak *(voice)*
   - Use Voice Activity *(voice)*
   - Use Slash Commands
4. Copy the generated URL (bottom of page), open it, pick your Discord
   server, authorize.

---

## First install

```bash
# 1. Create the namespace (one-time)
kubectl create namespace discord

# 2. Populate values.local.yaml with your secrets
cp discord/vocard/values.local.yaml.example discord/vocard/values.local.yaml
# ...edit discord/vocard/values.local.yaml:
#    - paste discordToken + discordClientId from Developer Portal
#    - generate mongoRootPassword + lavalinkPassword:
#        openssl rand -base64 24

# 3. Install
helm install vocard ./discord/vocard -n discord \
  -f discord/vocard/values.yaml \
  -f discord/vocard/values.local.yaml

# 4. Watch pods come up (expect 4 pods Running within ~1-2 min;
#    Lavalink takes longest while it downloads plugins on first boot)
kubectl -n discord get pods -w
```

---

## Upgrade

```bash
./discord/vocard/upgrade.sh
```

Wraps `helm upgrade` + waits for each component's rollout + tails the bot
logs. No pre-flight world-flush needed (unlike Minecraft) — Mongo commits
continuously, Lavalink is stateless, and the bot reconnects to Discord on
restart.

---

## Verification

```bash
# 1. Pods all Running
kubectl -n discord get pods
#   NAME                                READY   STATUS
#   vocard-bot-<hash>                   1/1     Running
#   vocard-lavalink-<hash>              1/1     Running
#   vocard-mongo-0                      1/1     Running
#   vocard-spotify-tokener-<hash>       1/1     Running

# 2. Bot connected to Discord — check logs for "Logged in as ..."
kubectl -n discord logs deployment/vocard-bot | grep -i "logged in"

# 3. In your Discord server, the bot shows Online.

# 4. Join a voice channel, then in any text channel:
#      /play never gonna give you up
#    → bot joins, starts playing. Try /skip, /queue, a Spotify URL.

# 5. Persistence check
kubectl -n discord delete pod -l app.kubernetes.io/component=bot
# Bot comes back, queue state survives in Mongo.
```

---

## Rotating secrets

Edit `discord/vocard/values.local.yaml`, then `./upgrade.sh`. The
`checksum/settings` annotation on the bot Deployment triggers a pod restart
whenever the rendered Secret changes. Same for Lavalink password changes
(`checksum/config` on the lavalink Deployment picks up the ConfigMap
change).

---

## Uninstall

```bash
helm uninstall vocard -n discord
# PVC for Mongo stays (so reinstall keeps history). To nuke queue data too:
kubectl -n discord delete pvc data-vocard-mongo-0
```

---

## Adding the dashboard later

The chart deliberately omits the Vocard-Dashboard web UI — slash commands
cover the "queue music together" use case fully, and the dashboard adds an
Ingress + Discord OAuth app that's nontrivial to get right. When you want
it:

1. **Second Discord OAuth app setup** — same portal, same app. Add a
   redirect URL under **OAuth2 → Redirects**:
   `https://<your-duckdns-host>/callback`. Copy the **Client Secret** from
   the same tab.
2. **Extend values.local.yaml** with dashboard creds (the stub `dashboard:
   enabled: false` in values.yaml is where the bool lives; flip it to
   `true` and add a `dashboard.secrets:` block):
   ```yaml
   dashboard:
     enabled: true
     host: vocard.<your-duckdns-host>
     secrets:
       clientSecret: "<paste from OAuth2 tab>"
       # Random strings, one each:
       secretKey: "<openssl rand -hex 32>"
       ipcPassword: "<openssl rand -base64 24>"
   ```
3. **Add the templates** — `dashboard-deployment.yaml`,
   `dashboard-service.yaml`, `dashboard-settings.yaml`,
   `dashboard-ingress.yaml`. The bot's `ipc_client.enable` flips to `true`
   when `dashboard.enabled: true` (already wired in
   `vocard-settings.yaml`).
4. **Ingress** — route `vocard.<your-duckdns-host>` through the talaria
   project's DuckDNS + cert-manager setup, pointing at
   `svc/vocard-dashboard:8000` in the `discord` namespace.

---

## Upstream

- Bot: <https://github.com/ChocoMeow/Vocard>
- Dashboard: <https://github.com/ChocoMeow/Vocard-Dashboard>
- Docs: <https://docs.vocard.xyz/>
- Lavalink: <https://github.com/lavalink-devs/Lavalink>
- LavaSrc plugin: <https://github.com/topi314/LavaSrc>
- Spotify tokener: <https://github.com/topi314/Spotify-Tokener>
