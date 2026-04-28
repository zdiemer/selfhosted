package com.zachd.claudemod.query;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.context.CommandContext;

import net.minecraft.block.entity.BlockEntity;
import net.minecraft.command.argument.IdentifierArgumentType;
import net.minecraft.inventory.Inventory;
import net.minecraft.item.Item;
import net.minecraft.item.ItemStack;
import net.minecraft.registry.Registries;
import net.minecraft.registry.RegistryKey;
import net.minecraft.registry.RegistryKeys;
import net.minecraft.registry.entry.RegistryEntryList;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.Identifier;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.ChunkPos;
import net.minecraft.world.World;
import net.minecraft.world.biome.Biome;
import net.minecraft.world.chunk.WorldChunk;
import net.minecraft.world.gen.structure.Structure;

import com.zachd.claudemod.ClaudeMod;
import com.zachd.claudemod.shared.ClaudeIo;

/**
 * Spatial queries: container scans, biome / structure locate, teleport home.
 *
 * Structure search is hybrid: scan loaded chunks first (free, watchdog-safe),
 * fall back to vanilla {@code locateStructure} with a hard-capped chunk
 * radius. Wider scans need to move off the server thread — earlier attempts
 * at 25–100 chunks took 50+ seconds and dropped TPS to ~1.8.
 */
public final class WorldSearchQueries {
    private WorldSearchQueries() {}

    // Cap container-search results.
    private static final int FIND_RESULT_CAP = 50;
    // Hard cap on chunks scanned per find query (defense against accidentally
    // touching every loaded chunk on a busy server).
    private static final int FIND_CHUNK_CAP = 2048;
    // View-distance halo (in chunks) we scan around each online player.
    private static final int FIND_VIEW_RADIUS = 8;

    private static final int BIOME_SEARCH_RADIUS_BLOCKS = 2048;
    private static final int LOADED_STRUCTURE_RADIUS_CHUNKS = 16;
    // Strict upper bound on the on-demand fallback when the loaded scan
    // misses. 5 chunks ≈ 80 blocks. Earlier 10-chunk attempts still cost
    // 52s of server-thread time on village searches in this pack (vanilla's
    // own timeout caught it, no watchdog kill, but TPS suffered). 5 keeps
    // the candidate spread set tiny — at most 0-1 points to verify.
    // Wider scans need to move off the server thread.
    private static final int ON_DEMAND_STRUCTURE_RADIUS_CHUNKS = 5;

    /**
     * Scan loaded chunks in the dimension for containers holding the item.
     * Iterates the view-distance halo around every online player; chunks
     * outside loaded memory aren't scanned (that would require touching
     * chunk files on disk, which is out of scope for an in-game query).
     * For "find iron in my base" this is enough since the asking player
     * is in their base.
     */
    public static int queryFind(CommandContext<ServerCommandSource> ctx) {
        Identifier dimId = IdentifierArgumentType.getIdentifier(ctx, "dim");
        String itemRaw = StringArgumentType.getString(ctx, "item").trim();

        Identifier itemId = Identifier.tryParse(itemRaw.contains(":") ? itemRaw : ("minecraft:" + itemRaw));
        if (itemId == null) return ClaudeIo.error(ctx, "bad item: " + itemRaw);
        if (!Registries.ITEM.containsId(itemId)) return ClaudeIo.error(ctx, "unknown item: " + itemId);
        Item target = Registries.ITEM.get(itemId);

        ServerWorld world = ctx.getSource().getServer().getWorld(
            RegistryKey.of(RegistryKeys.WORLD, dimId)
        );
        if (world == null) return ClaudeIo.error(ctx, "no world for dim: " + dimId);

        JsonArray hits = new JsonArray();
        java.util.Set<Long> visited = new java.util.HashSet<>();
        int hitCount = 0;
        int chunksScanned = 0;
        boolean truncated = false;

        outer:
        for (ServerPlayerEntity player : world.getPlayers()) {
            ChunkPos cp = player.getChunkPos();
            for (int dx = -FIND_VIEW_RADIUS; dx <= FIND_VIEW_RADIUS; dx++) {
                for (int dz = -FIND_VIEW_RADIUS; dz <= FIND_VIEW_RADIUS; dz++) {
                    long key = ChunkPos.toLong(cp.x + dx, cp.z + dz);
                    if (!visited.add(key)) continue;
                    if (chunksScanned++ >= FIND_CHUNK_CAP) { truncated = true; break outer; }
                    WorldChunk chunk = world.getChunkManager().getWorldChunk(cp.x + dx, cp.z + dz);
                    if (chunk == null) continue;
                    for (var entry : chunk.getBlockEntities().entrySet()) {
                        BlockEntity be = entry.getValue();
                        if (!(be instanceof Inventory inv)) continue;
                        int count = countItems(inv, target);
                        if (count <= 0) continue;
                        BlockPos bp = entry.getKey();
                        JsonObject hit = new JsonObject();
                        hit.addProperty("x", bp.getX());
                        hit.addProperty("y", bp.getY());
                        hit.addProperty("z", bp.getZ());
                        hit.addProperty("count", count);
                        Identifier beType = Registries.BLOCK_ENTITY_TYPE.getId(be.getType());
                        hit.addProperty("container", beType == null ? "?" : beType.toString());
                        hits.add(hit);
                        if (++hitCount >= FIND_RESULT_CAP) { truncated = true; break outer; }
                    }
                }
            }
        }

        JsonObject root = new JsonObject();
        root.addProperty("item", itemId.toString());
        root.addProperty("dim", dimId.toString());
        root.addProperty("hits", hitCount);
        root.addProperty("chunks_scanned", chunksScanned);
        if (truncated) root.addProperty("truncated", true);
        if (chunksScanned == 0) {
            root.addProperty("note", "no chunks loaded in this dimension (no players online here?)");
        }
        root.add("locations", hits);
        return ClaudeIo.reply(ctx, root);
    }

    private static int countItems(Inventory inv, Item target) {
        int count = 0;
        for (int i = 0; i < inv.size(); i++) {
            ItemStack stack = inv.getStack(i);
            if (stack.getItem() == target) count += stack.getCount();
        }
        return count;
    }

    public static int queryNearest(CommandContext<ServerCommandSource> ctx, boolean isBiome) {
        String idStr = StringArgumentType.getString(ctx, "id").trim();
        Identifier id = Identifier.tryParse(idStr.contains(":") ? idStr : "minecraft:" + idStr);
        if (id == null) return ClaudeIo.error(ctx, "bad id: " + idStr);

        ServerPlayerEntity p = ClaudeIo.onlinePlayer(ctx);
        if (p == null) return 0;
        ServerWorld world = (ServerWorld) p.getWorld();
        BlockPos origin = p.getBlockPos();

        JsonObject root = new JsonObject();
        root.addProperty(isBiome ? "biome" : "structure", id.toString());
        root.addProperty("player", p.getName().getString());
        JsonObject originJson = new JsonObject();
        originJson.addProperty("x", origin.getX());
        originJson.addProperty("y", origin.getY());
        originJson.addProperty("z", origin.getZ());
        originJson.addProperty("dim", world.getRegistryKey().getValue().toString());
        root.add("origin", originJson);

        try {
            BlockPos found;
            int radiusBlocks;
            if (isBiome) {
                var biomeReg = world.getRegistryManager().get(RegistryKeys.BIOME);
                RegistryKey<Biome> bk = RegistryKey.of(RegistryKeys.BIOME, id);
                if (!biomeReg.contains(bk)) return ClaudeIo.error(ctx, "unknown biome: " + id);
                var pair = world.locateBiome(
                    entry -> entry.matchesKey(bk),
                    origin, BIOME_SEARCH_RADIUS_BLOCKS, 32, 64
                );
                found = pair == null ? null : pair.getFirst();
                radiusBlocks = BIOME_SEARCH_RADIUS_BLOCKS;
            } else {
                var structReg = world.getRegistryManager().get(RegistryKeys.STRUCTURE);
                RegistryKey<Structure> sk =
                    RegistryKey.of(RegistryKeys.STRUCTURE, id);
                var entryOpt = structReg.getEntry(sk);
                if (entryOpt.isEmpty()) return ClaudeIo.error(ctx, "unknown structure: " + id);
                var target = entryOpt.get().value();
                // Step 1: scan already-loaded chunks (free, watchdog-safe).
                found = findLoadedStructure(world, origin, target);
                radiusBlocks = LOADED_STRUCTURE_RADIUS_CHUNKS * 16;
                // Step 2: if nothing in loaded chunks, try a tightly-bounded
                // on-demand vanilla locate. 10 chunks is small enough that
                // even with chunk loading the scan stays under a few
                // seconds in the typical case.
                if (found == null) {
                    var entryList = RegistryEntryList.of(entryOpt.get());
                    var pair = world.getChunkManager().getChunkGenerator()
                        .locateStructure(world, entryList, origin,
                                         ON_DEMAND_STRUCTURE_RADIUS_CHUNKS, false);
                    found = pair == null ? null : pair.getFirst();
                    if (found != null) radiusBlocks = ON_DEMAND_STRUCTURE_RADIUS_CHUNKS * 16;
                }
            }
            if (found == null) {
                root.addProperty("found", false);
                root.addProperty("note", isBiome
                    ? "no match within " + radiusBlocks + " blocks"
                    : "no match in any currently-loaded chunk near you (we don't on-demand-load chunks here — it can crash the server). For first-time discovery, run vanilla `/locate structure " + id + "` yourself.");
            } else {
                root.addProperty("found", true);
                root.addProperty("x", found.getX());
                root.addProperty("y", found.getY());
                root.addProperty("z", found.getZ());
                double dx = found.getX() - origin.getX();
                double dz = found.getZ() - origin.getZ();
                root.addProperty("distance_blocks", (int) Math.sqrt(dx * dx + dz * dz));
            }
        } catch (Throwable t) {
            ClaudeMod.LOG.warn("nearest lookup failed: {}", t.toString());
            return ClaudeIo.error(ctx, "lookup failed: " + t.getClass().getSimpleName());
        }
        return ClaudeIo.reply(ctx, root);
    }

    /**
     * Walk the LOADED chunk halo around origin and return the closest
     * BlockPos that has a structure start matching target. No chunk
     * loading — only chunks currently held in memory by the server are
     * inspected. Returns null if no match is loaded.
     */
    private static BlockPos findLoadedStructure(ServerWorld world, BlockPos origin, Structure target) {
        BlockPos best = null;
        double bestDist = Double.MAX_VALUE;
        int cx0 = origin.getX() >> 4;
        int cz0 = origin.getZ() >> 4;
        for (int dx = -LOADED_STRUCTURE_RADIUS_CHUNKS; dx <= LOADED_STRUCTURE_RADIUS_CHUNKS; dx++) {
            for (int dz = -LOADED_STRUCTURE_RADIUS_CHUNKS; dz <= LOADED_STRUCTURE_RADIUS_CHUNKS; dz++) {
                WorldChunk chunk = world.getChunkManager().getWorldChunk(cx0 + dx, cz0 + dz);
                if (chunk == null) continue;
                var start = chunk.getStructureStart(target);
                if (start == null || !start.hasChildren()) continue;
                var box = start.getBoundingBox();
                BlockPos centre = new BlockPos(
                    (box.getMinX() + box.getMaxX()) / 2,
                    (box.getMinY() + box.getMaxY()) / 2,
                    (box.getMinZ() + box.getMaxZ()) / 2
                );
                double d = origin.getSquaredDistance(centre);
                if (d < bestDist) {
                    bestDist = d;
                    best = centre;
                }
            }
        }
        return best;
    }

    /**
     * Teleport a player to their bed/respawn point. RCON-only, intended to
     * be invoked by the bridge tool teleport_caller_home which substitutes
     * the asking player's name from CALLER_PLAYER. Returns a structured
     * error if the player has no spawn set (never slept in a bed).
     */
    public static int homeCommand(CommandContext<ServerCommandSource> ctx) {
        ServerPlayerEntity p = ClaudeIo.onlinePlayer(ctx);
        if (p == null) return 0;

        BlockPos spawn = p.getSpawnPointPosition();
        if (spawn == null) {
            JsonObject err = new JsonObject();
            err.addProperty("ok", false);
            err.addProperty("error", "no spawn point set; sleep in a bed first");
            return ClaudeIo.reply(ctx, err);
        }
        RegistryKey<World> dim = p.getSpawnPointDimension();
        ServerWorld target = ctx.getSource().getServer().getWorld(dim);
        if (target == null) {
            JsonObject err = new JsonObject();
            err.addProperty("ok", false);
            err.addProperty("error", "spawn dimension unavailable: " + dim.getValue());
            return ClaudeIo.reply(ctx, err);
        }
        float angle = p.getSpawnAngle();
        // +0.5 to land in the block centre; +1 on Y to clear bed/anchor block.
        p.teleport(target, spawn.getX() + 0.5, spawn.getY() + 1, spawn.getZ() + 0.5, angle, 0);

        JsonObject ok = new JsonObject();
        ok.addProperty("ok", true);
        ok.addProperty("player", p.getName().getString());
        ok.addProperty("x", spawn.getX());
        ok.addProperty("y", spawn.getY());
        ok.addProperty("z", spawn.getZ());
        ok.addProperty("dim", dim.getValue().toString());
        return ClaudeIo.reply(ctx, ok);
    }
}
