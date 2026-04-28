package com.zachd.claudemod.query;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.context.CommandContext;

import net.minecraft.entity.boss.BossBar;
import net.minecraft.entity.boss.CommandBossBar;
import net.minecraft.entity.boss.BossBarManager;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.text.Text;
import net.minecraft.util.Identifier;

import com.zachd.claudemod.ClaudeMod;
import com.zachd.claudemod.shared.ClaudeIo;

/**
 * Server admin / live-feedback queries:
 *  - {@code claudemod query perf}: tick-time histogram, per-dim load, JVM heap.
 *  - {@code claudemod bossbar update|remove}: silent bossbar manipulation that
 *    bypasses vanilla's Brigadier feedback path so the bridge can update a
 *    progress indicator without spamming server logs.
 */
public final class ServerAdminQueries {
    private ServerAdminQueries() {}

    /**
     * Server perf snapshot: average MSPT (ms per tick) → derived TPS,
     * per-dimension loaded chunk counts, entity counts, online players,
     * and JVM heap usage.
     */
    public static int queryPerf(CommandContext<ServerCommandSource> ctx) {
        MinecraftServer server = ctx.getSource().getServer();
        JsonObject root = new JsonObject();

        // Tick perf — averaged over our rolling history filled by the
        // ServerTickEvents handlers in ClaudeMod.
        int fill = ClaudeMod.tickHistoryFill;
        if (fill > 0) {
            long sum = 0;
            for (int i = 0; i < fill; i++) sum += ClaudeMod.TICK_HISTORY_NS[i];
            double avgMs = (sum / (double) fill) / 1_000_000.0;
            root.addProperty("avg_mspt", avgMs);
            root.addProperty("tps", Math.min(20.0, 1000.0 / Math.max(avgMs, 0.0001)));
            root.addProperty("tick_samples", fill);
        }

        // Per-world counts. We skip dims with no loaded chunks AND no
        // entities AND no players to keep the response focused on
        // dimensions actually doing work — this pack registers ~25 dims
        // (ad_astra, mineCells, etc.), most of which sit empty.
        JsonArray worlds = new JsonArray();
        long totalEntities = 0;
        long totalChunks = 0;
        int idleDims = 0;
        for (ServerWorld world : server.getWorlds()) {
            int chunks = world.getChunkManager().getLoadedChunkCount();
            int entities = 0;
            for (var ignored : world.iterateEntities()) entities++;
            int players = world.getPlayers().size();
            totalEntities += entities;
            totalChunks += chunks;
            if (chunks == 0 && entities == 0 && players == 0) {
                idleDims++;
                continue;
            }
            JsonObject w = new JsonObject();
            w.addProperty("dim", world.getRegistryKey().getValue().toString());
            w.addProperty("loaded_chunks", chunks);
            w.addProperty("entities", entities);
            w.addProperty("players", players);
            worlds.add(w);
        }
        root.add("active_worlds", worlds);
        root.addProperty("idle_dims", idleDims);
        root.addProperty("total_entities", totalEntities);
        root.addProperty("total_loaded_chunks", totalChunks);
        root.addProperty("online_players", server.getCurrentPlayerCount());

        // JVM heap.
        Runtime rt = Runtime.getRuntime();
        long total = rt.totalMemory();
        long free = rt.freeMemory();
        long max = rt.maxMemory();
        JsonObject mem = new JsonObject();
        mem.addProperty("used_mb", (total - free) / (1024 * 1024));
        mem.addProperty("committed_mb", total / (1024 * 1024));
        mem.addProperty("max_mb", max / (1024 * 1024));
        mem.addProperty("used_pct", Math.round(((total - free) * 100.0) / max));
        root.add("jvm_heap", mem);

        return ClaudeIo.reply(ctx, root);
    }

    // ---------- bossbar (silent — no log spam) ------------------------------
    // The vanilla `bossbar` family routes through Brigadier and emits
    // "[Rcon: Set ... for custom bossbar X]" lines on every set, which
    // spams /data/logs because the bridge issues five of them per
    // progress update. We bypass that by talking to BossBarManager
    // directly and never calling sendFeedback.

    private static final BossBar.Color BOSSBAR_COLOR = BossBar.Color.BLUE;

    private static Identifier bossbarId(String player) {
        String safe = player.toLowerCase().replaceAll("[^a-z0-9_]", "_");
        return new Identifier("claudemod", "claude_" + safe);
    }

    public static int bossbarUpdate(CommandContext<ServerCommandSource> ctx) {
        String playerName = StringArgumentType.getString(ctx, "player");
        String text = StringArgumentType.getString(ctx, "text");
        MinecraftServer server = ctx.getSource().getServer();
        ServerPlayerEntity p = server.getPlayerManager().getPlayer(playerName);
        if (p == null) return 0;

        Identifier id = bossbarId(playerName);
        BossBarManager manager = server.getBossBarManager();
        CommandBossBar bar = manager.get(id);
        if (bar == null) {
            bar = manager.add(id, Text.literal(text));
            bar.setColor(BOSSBAR_COLOR);
            bar.setMaxValue(1);
            bar.setValue(1);
        } else {
            bar.setName(Text.literal(text));
        }
        if (!bar.getPlayers().contains(p)) {
            bar.addPlayer(p);
        }
        return 1;
    }

    public static int bossbarRemove(CommandContext<ServerCommandSource> ctx) {
        String playerName = StringArgumentType.getString(ctx, "player");
        Identifier id = bossbarId(playerName);
        BossBarManager manager = ctx.getSource().getServer().getBossBarManager();
        CommandBossBar bar = manager.get(id);
        if (bar != null) {
            // clearPlayers sends the REMOVE packet to attached clients so
            // they actually hide the bar; manager.remove alone just drops
            // it from the registry without notifying anyone.
            bar.clearPlayers();
            manager.remove(bar);
        }
        return 1;
    }
}
