# Client Setup — Prominence II: Hasturian Era

This server now runs the **Prominence II: Hasturian Era** Fabric modpack
(Minecraft 1.20.1). Vanilla launchers can no longer connect — every player
must install the matching client modpack. The server address is in the
invite you received.

## Requirements

- A legitimate Minecraft: Java Edition account.
- **8 GB RAM** allocated to the Minecraft client (the modpack is ~400 mods).
  Your machine needs at least 16 GB total for this to be comfortable.
- **Java 17** — every launcher below will install/manage this for you; you
  don't need a system Java install.
- ~5 GB free disk space for the modpack and its assets.

## Install — pick one launcher

### Option 1 · Modrinth App (recommended)

Easiest path. One-click modpack install, automatic updates when the server
bumps versions.

1. Download **Modrinth App** from <https://modrinth.com/app>. Install and
   sign in with your Microsoft / Mojang account.
2. Open the **Browse** tab, search for `Prominence II Hasturian Era`, and
   click the result whose platform is **Fabric**.
3. Click **Install**. Wait for it to download (several hundred MB).
4. On the installed instance tile, click the **⋯** menu → **Options** →
   **Java and memory**. Set **Allocated memory** to `8192 MB`. Save.
5. Click **Play** once to let it finish first-launch setup and reach the
   title screen, then quit.
6. Click **Play** again → **Multiplayer** → **Add Server**, paste the
   server address, and **Done**. Join.

### Option 2 · Prism Launcher

Good if you already use Prism, or want finer control over Java args.

1. Install Prism Launcher from <https://prismlauncher.org/>.
2. **Add Instance** → **Modrinth** tab → search `Prominence II Hasturian
   Era` → pick the **Fabric** variant → **OK**.
3. Right-click the instance → **Edit** → **Settings** → **Java** → raise
   max memory to `8192 MB`.
4. Launch, hit the title screen once, then **Multiplayer** → **Add Server**.

### Option 3 · CurseForge App

Use this if the Modrinth variant misbehaves on your machine — CurseForge
hosts the same pack under a slightly different build.

1. Install **CurseForge** from <https://www.curseforge.com/download/app>.
2. **Browse Modpacks** → search `Prominence II Hasturian Era` → install.
3. **Settings** (cog icon) → **Minecraft** → raise **Allocated Memory** to
   `8192 MB`.
4. Launch once, then add the server from the Multiplayer menu.

## Troubleshooting

**"Outdated server" or "Outdated client" on connect.** Your modpack version
doesn't match the server's. Update the modpack from inside your launcher
(Modrinth App: the instance tile shows an ↻ icon; Prism: right-click →
Edit → Version → Change Version; CurseForge: the instance card shows an
update prompt). If the server moved to a newer version and yours is stuck,
ping the admin.

**Game crashes on launch / out of memory.** Bump allocated RAM to 10 GB if
your system has the headroom. If it still crashes, check the launcher's
log — a mod conflict will name the offending mod in the stack trace.

**"Fabric Loader X.Y.Z required."** Your launcher didn't install the right
Fabric Loader version. In Modrinth App, delete the instance and reinstall.
In Prism, **Edit → Version → Install Fabric**, and let it pick the version
the modpack declares.

**Can't see the server in the list.** Make sure you're on the Fabric
variant of the pack (not the "Forge" or "RPG Legacy" variants — the server
only accepts the `prominence-2-fabric` build).

**Connection times out.** Double-check the server address from the invite.
If it's still failing, the server may be auto-paused — it wakes up on the
first connection attempt but that attempt itself may time out. Retry once
and you should get in.
