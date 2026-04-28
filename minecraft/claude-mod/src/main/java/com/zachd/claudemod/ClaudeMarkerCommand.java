package com.zachd.claudemod;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.reflect.TypeToken;
import com.mojang.brigadier.CommandDispatcher;
import com.mojang.brigadier.arguments.DoubleArgumentType;
import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.context.CommandContext;

import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.command.CommandManager;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.text.Text;
import net.minecraft.util.WorldSavePath;

import de.bluecolored.bluemap.api.BlueMapAPI;
import de.bluecolored.bluemap.api.markers.MarkerSet;
import de.bluecolored.bluemap.api.markers.POIMarker;

/**
 * /claudemod mark add|list|remove — manages persistent POI markers that
 * appear on the live BlueMap web view.
 *
 * Markers are persisted to <world>/claudemod-markers.json (so they survive
 * server restarts and are included in mc-backup snapshots) and pushed into
 * BlueMap's in-process MarkerSet via its plugin API. On server start, we
 * load the JSON and call BlueMapAPI.onEnable to (re-)apply the markers
 * once BlueMap finishes booting.
 *
 * Routes (RCON-only, gated by !src.isExecutedByPlayer()):
 *   /claudemod mark add <name> <world> <x> <y> <z> <author> <label>
 *   /claudemod mark list
 *   /claudemod mark remove <name>
 *
 * Marker name must be unique. The bridge enforces "no spaces in name"
 * before calling. label can be any text. author is the player who
 * requested the marker (the bridge fills this from CALLER_PLAYER).
 */
public final class ClaudeMarkerCommand {
    private ClaudeMarkerCommand() {}

    private static final Gson GSON = new GsonBuilder().setPrettyPrinting().disableHtmlEscaping().create();
    private static final String SET_ID = "claudemod-pois";
    private static final String SET_LABEL = "Player POIs";

    // In-memory marker state. Mirrors the JSON file. Synchronized on `markers`
    // because Brigadier handlers can run on the server thread while the
    // BlueMap onEnable callback may run on its own thread.
    private static final List<MarkerEntry> markers = Collections.synchronizedList(new ArrayList<>());
    private static volatile Path stateFile = null;

    /** Persistent shape — what gets written to the JSON file. */
    public static class MarkerEntry {
        public String name;
        public String world;     // e.g. "minecraft:overworld"
        public double x, y, z;
        public String author;
        public String label;
        public long created_at;
    }

    public static void register(CommandDispatcher<ServerCommandSource> dispatcher) {
        dispatcher.register(
            CommandManager.literal("claudemod")
                .requires(src -> !src.isExecutedByPlayer())
                .then(CommandManager.literal("mark")
                    .then(CommandManager.literal("add")
                        .then(CommandManager.argument("name", StringArgumentType.word())
                            .then(CommandManager.argument("world",
                                    net.minecraft.command.argument.IdentifierArgumentType.identifier())
                                .then(CommandManager.argument("x", DoubleArgumentType.doubleArg())
                                    .then(CommandManager.argument("y", DoubleArgumentType.doubleArg())
                                        .then(CommandManager.argument("z", DoubleArgumentType.doubleArg())
                                            .then(CommandManager.argument("author", StringArgumentType.word())
                                                .then(CommandManager.argument("label", StringArgumentType.greedyString())
                                                    .executes(ClaudeMarkerCommand::add)))))))))
                    .then(CommandManager.literal("list")
                        .executes(ClaudeMarkerCommand::list))
                    .then(CommandManager.literal("remove")
                        .then(CommandManager.argument("name", StringArgumentType.word())
                            .executes(ClaudeMarkerCommand::remove))))
        );
    }

    /**
     * Called from ClaudeMod#onServerStarted. Loads the JSON file and
     * registers a BlueMap onEnable callback that syncs markers into the
     * map's MarkerSet. Both steps are no-ops if BlueMap isn't installed —
     * the JSON state still works for list/remove via RCON.
     */
    public static void onServerStarted(MinecraftServer server) {
        Path worldRoot = server.getSavePath(WorldSavePath.ROOT);
        stateFile = worldRoot.resolve("claudemod-markers.json");
        loadMarkers();

        if (FabricLoader.getInstance().isModLoaded("bluemap")) {
            try {
                BlueMapAPI.onEnable(ClaudeMarkerCommand::syncToBlueMap);
                ClaudeMod.LOG.info("registered BlueMap onEnable hook ({} marker(s) queued)", markers.size());
            } catch (Throwable t) {
                ClaudeMod.LOG.warn("BlueMap onEnable registration failed: {}", t.toString());
            }
        } else {
            ClaudeMod.LOG.info("BlueMap mod not detected; markers will live in JSON only");
        }
    }

    // ---------- Brigadier handlers ------------------------------------------
    private static int add(CommandContext<ServerCommandSource> ctx) {
        String name = StringArgumentType.getString(ctx, "name");
        // IdentifierArgumentType always returns a namespaced id, so no
        // "missing namespace" branch needed.
        String world = net.minecraft.command.argument.IdentifierArgumentType
            .getIdentifier(ctx, "world").toString();
        double x = DoubleArgumentType.getDouble(ctx, "x");
        double y = DoubleArgumentType.getDouble(ctx, "y");
        double z = DoubleArgumentType.getDouble(ctx, "z");
        String author = StringArgumentType.getString(ctx, "author");
        String label = StringArgumentType.getString(ctx, "label");

        synchronized (markers) {
            // Replace existing marker of the same name.
            markers.removeIf(m -> m.name.equalsIgnoreCase(name));
            MarkerEntry e = new MarkerEntry();
            e.name = name;
            e.world = world;
            e.x = x; e.y = y; e.z = z;
            e.author = author;
            e.label = label;
            e.created_at = System.currentTimeMillis() / 1000L;
            markers.add(e);
        }
        saveMarkers();
        BlueMapAPI.getInstance().ifPresent(ClaudeMarkerCommand::syncToBlueMap);

        JsonObject ack = new JsonObject();
        ack.addProperty("ok", true);
        ack.addProperty("name", name);
        ack.addProperty("world", world);
        ack.addProperty("x", x); ack.addProperty("y", y); ack.addProperty("z", z);
        ack.addProperty("label", label);
        return reply(ctx, ack);
    }

    private static int list(CommandContext<ServerCommandSource> ctx) {
        JsonObject root = new JsonObject();
        JsonArray arr = new JsonArray();
        synchronized (markers) {
            for (MarkerEntry m : markers) {
                JsonObject o = new JsonObject();
                o.addProperty("name", m.name);
                o.addProperty("world", m.world);
                o.addProperty("x", m.x); o.addProperty("y", m.y); o.addProperty("z", m.z);
                o.addProperty("author", m.author);
                o.addProperty("label", m.label);
                o.addProperty("created_at", m.created_at);
                arr.add(o);
            }
        }
        root.addProperty("count", arr.size());
        root.add("markers", arr);
        return reply(ctx, root);
    }

    private static int remove(CommandContext<ServerCommandSource> ctx) {
        String name = StringArgumentType.getString(ctx, "name");
        boolean removed;
        synchronized (markers) {
            removed = markers.removeIf(m -> m.name.equalsIgnoreCase(name));
        }
        if (removed) {
            saveMarkers();
            BlueMapAPI.getInstance().ifPresent(ClaudeMarkerCommand::syncToBlueMap);
        }
        JsonObject ack = new JsonObject();
        ack.addProperty("ok", removed);
        ack.addProperty("name", name);
        if (!removed) ack.addProperty("error", "no marker with that name");
        return reply(ctx, ack);
    }

    // ---------- BlueMap sync ------------------------------------------------
    /**
     * Push the in-memory marker list into BlueMap's MarkerSet for every map
     * of every matching world. We keep a dedicated MarkerSet ("claudemod-pois")
     * that we own entirely — clearing and rebuilding it on every sync is
     * fine because the canonical state is the JSON file.
     */
    private static void syncToBlueMap(BlueMapAPI api) {
        try {
            for (var world : api.getWorlds()) {
                for (var map : world.getMaps()) {
                    MarkerSet set = map.getMarkerSets()
                        .computeIfAbsent(SET_ID, k -> MarkerSet.builder().label(SET_LABEL).build());
                    set.getMarkers().clear();
                    synchronized (markers) {
                        for (MarkerEntry e : markers) {
                            // V1: render every marker on every map. Cross-world
                            // matching is fragile (BlueMap world IDs ≠ MC dim
                            // identifiers) and will be tightened later.
                            POIMarker m = POIMarker.builder()
                                .position(e.x, e.y, e.z)
                                .label(e.label != null && !e.label.isEmpty() ? e.label : e.name)
                                .detail("Set by " + e.author)
                                .build();
                            set.put(e.name, m);
                        }
                    }
                }
            }
        } catch (Throwable t) {
            ClaudeMod.LOG.warn("BlueMap marker sync failed: {}", t.toString());
        }
    }

    // ---------- persistence -------------------------------------------------
    private static void loadMarkers() {
        if (stateFile == null || !Files.exists(stateFile)) return;
        try {
            String json = Files.readString(stateFile);
            List<MarkerEntry> loaded = GSON.fromJson(
                json, new TypeToken<List<MarkerEntry>>(){}.getType()
            );
            if (loaded != null) {
                synchronized (markers) {
                    markers.clear();
                    markers.addAll(loaded);
                }
                ClaudeMod.LOG.info("loaded {} marker(s) from {}", markers.size(), stateFile);
            }
        } catch (IOException e) {
            ClaudeMod.LOG.warn("failed to read markers file {}: {}", stateFile, e.toString());
        } catch (Throwable t) {
            ClaudeMod.LOG.warn("markers file is corrupt; starting empty: {}", t.toString());
        }
    }

    private static void saveMarkers() {
        if (stateFile == null) return;
        try {
            Files.createDirectories(stateFile.getParent());
            List<MarkerEntry> snapshot;
            synchronized (markers) {
                snapshot = new ArrayList<>(markers);
            }
            Path tmp = stateFile.resolveSibling(stateFile.getFileName().toString() + ".tmp");
            Files.writeString(tmp, GSON.toJson(snapshot));
            Files.move(tmp, stateFile, java.nio.file.StandardCopyOption.REPLACE_EXISTING,
                       java.nio.file.StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException e) {
            ClaudeMod.LOG.error("failed to write markers file {}: {}", stateFile, e.toString());
        }
    }

    // ---------- helpers -----------------------------------------------------
    private static int reply(CommandContext<ServerCommandSource> ctx, JsonObject o) {
        final String json = GSON.toJson(o);
        ctx.getSource().sendFeedback(() -> Text.literal(json), false);
        return 1;
    }
}
