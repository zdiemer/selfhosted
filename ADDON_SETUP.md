# Add-on setup — BlueMap & Discord Integration

The two Modrinth add-ons listed in `values.yaml` (`bluemap`, `dcintegration`)
are installed automatically on every pod start, but both need a one-time
manual config touch after the first successful boot. Until those touches
land, BlueMap refuses to start and Discord Integration sits idle without a
bot token.

All commands below assume `kubectl` is pointed at the k3s cluster and the
release is named `mc` in namespace `minecraft`.

```bash
export POD=$(kubectl -n minecraft get pod -l app=mc-minecraft \
  -o jsonpath='{.items[0].metadata.name}')
echo "$POD"   # should print e.g. mc-minecraft-5bd694b444-9t8rj
```

(The first line is silent on success — assignment doesn't print. The
`echo` is just to confirm `$POD` got set.)

## 1 · Discord Integration (dcintegration)

### 1a. Create the Discord bot (one-time, do this first)

1. Go to <https://discord.com/developers/applications> → **New Application**
   → name it (e.g. "Hasturian Era Bridge").
2. **Bot** tab → **Reset Token** → copy the token. It's shown exactly once
   — save it somewhere temporary.
3. Still on the **Bot** tab, enable all three **Privileged Gateway Intents**:
   - Presence Intent
   - Server Members Intent
   - Message Content Intent
4. **OAuth2** tab → **URL Generator**. This page has two lists:
   - **Scopes** (top, `resource.verb` style) — tick **only** `bot`. Ignore
     `guilds.messages.read` and friends; those are for user-level OAuth
     apps, not server bots.
   - **Bot Permissions** (appears *below* Scopes once `bot` is ticked,
     plain-English checkboxes — same set as the Bot tab) — tick
     `View Channels`, `Send Messages`, `Read Message History`,
     `Manage Webhooks`.
5. Copy the generated URL from the bottom of the page, open it in a
   browser, and invite the bot to your Discord server.
6. In Discord: **User Settings** → **Advanced** → **Developer Mode** on.
   Right-click the target channel → **Copy Channel ID**. Save that too.

### 1b. Paste token + channel ID into server config

The mod generates a single `/data/config/Discord-Integration.toml` on
first boot. The relevant defaults are:

```toml
[general]
  botToken = "INSERT BOT TOKEN HERE"
  botChannel = "000000000"
```

Edit them in place:

```bash
# Confirm the file exists and inspect defaults
kubectl -n minecraft exec "$POD" -c mc-minecraft -- \
  head -8 /data/config/Discord-Integration.toml

# Paste bot token
kubectl -n minecraft exec "$POD" -c mc-minecraft -- \
  sed -i 's|botToken = "INSERT BOT TOKEN HERE"|botToken = "PASTE_TOKEN"|' \
  /data/config/Discord-Integration.toml

# Paste the bridge channel ID
kubectl -n minecraft exec "$POD" -c mc-minecraft -- \
  sed -i 's|botChannel = "000000000"|botChannel = "PASTE_CHANNEL_ID"|' \
  /data/config/Discord-Integration.toml
```

If the `sed` commands don't match (defaults drift across mod versions),
`kubectl cp` the file out, edit locally, and `kubectl cp` it back.

Restart the pod to pick up the new token:

```bash
kubectl -n minecraft rollout restart deploy/mc-minecraft
```

**Verify:** the bot should show as Online in your Discord and post a
startup message in the bridge channel within ~30s of the pod becoming
Ready.

## 2 · BlueMap

BlueMap refuses to run on first boot until you explicitly opt in to its
one-time download of web assets from its own CDN. This is a license/legal
flag, not a network setting.

### 2a. Accept the download

```bash
kubectl -n minecraft exec "$POD" -c mc-minecraft -- \
  sed -i 's|accept-download: false|accept-download: true|' \
  /data/config/bluemap/core.conf
```

### 2b. (Optional) Configure rendering

The default `bluemap/maps/*.conf` covers overworld, nether, and end at
reasonable zoom levels. If you want to tweak render distance or exclude
regions, edit files under `/data/config/bluemap/maps/` — they're small
and well-commented.

### 2c. Restart

```bash
kubectl -n minecraft rollout restart deploy/mc-minecraft
```

On the next boot BlueMap downloads its web assets, then spawns a worker
that progressively renders the existing world into tile PNGs under
`/data/bluemap/`. Expect the initial render to chew CPU for a while on a
fresh world; it converges to zero once all chunks are rendered.

### 2d. Reach the web UI

The Helm chart exposes port 8100 via a second LoadBalancer service:

```bash
kubectl -n minecraft get svc mc-minecraft-bluemap
# EXTERNAL-IP column shows node IPs (klipper-lb); browse http://<ip>:8100
```

For a public URL with TLS, wire `map.<domain>` into the **talaria** sibling
project's DuckDNS ingress. Point it at `svc/mc-minecraft-bluemap:8100` in
the `minecraft` namespace; the pattern matches anything else talaria
already fronts.

## Troubleshooting

**Discord bot shows Online but doesn't mirror chat.** Check the **Message
Content Intent** toggle on the Developer Portal — it's opt-in and easy to
miss. Without it, the bot sees events but not message bodies.

**Discord bot stays Offline.** Token is wrong or intents are disabled.
Re-copy the token (it's shown exactly once on reset) and re-verify all
three intents are on.

**BlueMap logs "webserver disabled" after restart.** Double-check
`/data/config/bluemap/webserver.conf` — `enabled: true` and
`bind-address: "0.0.0.0"`. The default bind is often `127.0.0.1`, which
the in-pod webserver then refuses external traffic on.

**BlueMap map is empty / black.** Render hasn't caught up yet. RCON into
the container and run `/bluemap render` to force a full re-render of the
loaded chunks:

```bash
kubectl -n minecraft exec -it "$POD" -c mc-minecraft -- \
  rcon-cli bluemap render
```

**Neither add-on appears in `/data/mods` after boot.** itzg skipped
`MODRINTH_PROJECTS` — check pod logs for "Could not find loader" or
version-resolution errors, and confirm `MODRINTH_LOADER=fabric` is still
set in `values.yaml`. As a manual fallback, download the Fabric 1.20.1
jars from Modrinth and `kubectl cp` them into `/data/mods/` then restart.
