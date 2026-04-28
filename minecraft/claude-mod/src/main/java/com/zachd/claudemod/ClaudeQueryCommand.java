package com.zachd.claudemod;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.mojang.brigadier.CommandDispatcher;
import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.context.CommandContext;

import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.block.entity.BlockEntity;
import net.minecraft.enchantment.EnchantmentHelper;
import net.minecraft.entity.player.PlayerInventory;
import net.minecraft.inventory.Inventory;
import net.minecraft.item.Item;
import net.minecraft.item.ItemStack;
import net.minecraft.recipe.Ingredient;
import net.minecraft.recipe.Recipe;
import net.minecraft.recipe.ShapedRecipe;
import net.minecraft.recipe.ShapelessRecipe;
import net.minecraft.recipe.AbstractCookingRecipe;
import net.minecraft.registry.Registries;
import net.minecraft.registry.RegistryKey;
import net.minecraft.registry.RegistryKeys;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.command.CommandManager;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.text.Text;
import net.minecraft.util.Identifier;
import net.minecraft.util.WorldSavePath;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.ChunkPos;
import net.minecraft.world.World;
import net.minecraft.world.chunk.WorldChunk;

/**
 * RCON-only Brigadier subcommands that expose game state Claude can't read
 * via vanilla commands alone. All output is JSON via sendFeedback so the
 * bridge's MCP layer gets a deterministic shape on the RCON wire.
 *
 * Routes (all gated to non-player sources):
 *   /claudemod query inventory <player>
 *   /claudemod query xp        <player>
 *   /claudemod query stats     <player> [type]
 *   /claudemod query recipes makes <item_id>
 *   /claudemod query recipes uses  <item_id>
 *
 * Response shape per query is documented inline at each method.
 */
public final class ClaudeQueryCommand {
    private ClaudeQueryCommand() {}

    private static final Gson GSON = new GsonBuilder().disableHtmlEscaping().create();
    // RCON output cap. Minecraft RCON spec is 4096; we leave headroom for
    // the network framing the protocol adds.
    private static final int MAX_RESPONSE_CHARS = 3500;
    // Per-category cap on stat entries returned, sorted by count desc.
    // Modded servers have thousands of stat keys; capping keeps the response
    // under the RCON budget.
    private static final int STATS_TOP_N = 30;
    // Cap recipe results.
    private static final int RECIPE_RESULT_CAP = 20;
    // Cap container-search results.
    private static final int FIND_RESULT_CAP = 50;
    // Hard cap on chunks scanned per find query (defense against accidentally
    // touching every loaded chunk on a busy server).
    private static final int FIND_CHUNK_CAP = 2048;
    // View-distance halo (in chunks) we scan around each online player.
    private static final int FIND_VIEW_RADIUS = 8;
    // Cap on quest results returned per search.
    private static final int QUEST_HIT_CAP = 15;

    public static void register(CommandDispatcher<ServerCommandSource> dispatcher) {
        dispatcher.register(
            CommandManager.literal("claudemod")
                .requires(src -> !src.isExecutedByPlayer())
                .then(CommandManager.literal("query")
                    .then(CommandManager.literal("inventory")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ClaudeQueryCommand::queryInventory)))
                    .then(CommandManager.literal("xp")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ClaudeQueryCommand::queryXp)))
                    .then(CommandManager.literal("stats")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ctx -> queryStats(ctx, null))
                            .then(CommandManager.argument("type", StringArgumentType.word())
                                .executes(ctx -> queryStats(ctx, StringArgumentType.getString(ctx, "type"))))))
                    .then(CommandManager.literal("recipes")
                        .then(CommandManager.literal("makes")
                            .then(CommandManager.argument("item", StringArgumentType.greedyString())
                                .executes(ctx -> queryRecipes(ctx, true))))
                        .then(CommandManager.literal("uses")
                            .then(CommandManager.argument("item", StringArgumentType.greedyString())
                                .executes(ctx -> queryRecipes(ctx, false)))))
                    .then(CommandManager.literal("trinkets")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ClaudeQueryCommand::queryTrinkets)))
                    .then(CommandManager.literal("quest")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ctx -> queryQuest(ctx, null))
                            .then(CommandManager.argument("search", StringArgumentType.greedyString())
                                .executes(ctx -> queryQuest(ctx, StringArgumentType.getString(ctx, "search"))))))
                    .then(CommandManager.literal("find")
                        .then(CommandManager.argument("dim", StringArgumentType.word())
                            .then(CommandManager.argument("item", StringArgumentType.greedyString())
                                .executes(ClaudeQueryCommand::queryFind))))
                    .then(CommandManager.literal("nbt_keys")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ClaudeQueryCommand::queryNbtKeys)))
                    .then(CommandManager.literal("skills")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ClaudeQueryCommand::querySkills))))
        );
    }

    // ---------- inventory ----------------------------------------------------
    /**
     * { "player":"X", "main_hand":{...}, "off_hand":{...},
     *   "armor":{"head":{...}, "chest":{...}, "legs":{...}, "feet":{...}},
     *   "hotbar":[{...} x9], "main":[{...} x27], "ender_chest":[{...}] }
     * Each stack: { "id":"minecraft:stone", "count":42,
     *               "name":"<custom display name or null>",
     *               "enchants":["sharpness V","unbreaking III"] }
     */
    private static int queryInventory(CommandContext<ServerCommandSource> ctx) {
        ServerPlayerEntity p = onlinePlayer(ctx);
        if (p == null) return 0;

        PlayerInventory inv = p.getInventory();
        JsonObject root = new JsonObject();
        root.addProperty("player", p.getName().getString());
        root.add("main_hand", stackJson(p.getMainHandStack()));
        root.add("off_hand", stackJson(p.getOffHandStack()));

        JsonObject armor = new JsonObject();
        armor.add("feet", stackJson(inv.armor.get(0)));
        armor.add("legs", stackJson(inv.armor.get(1)));
        armor.add("chest", stackJson(inv.armor.get(2)));
        armor.add("head", stackJson(inv.armor.get(3)));
        root.add("armor", armor);

        JsonArray hotbar = new JsonArray();
        for (int i = 0; i < 9; i++) hotbar.add(stackJson(inv.getStack(i)));
        root.add("hotbar", hotbar);

        JsonArray main = new JsonArray();
        for (int i = 9; i < 36; i++) {
            JsonElement el = stackJson(inv.getStack(i));
            if (!el.isJsonNull()) main.add(el);
        }
        root.add("main", main);

        JsonArray ender = new JsonArray();
        for (int i = 0; i < p.getEnderChestInventory().size(); i++) {
            JsonElement el = stackJson(p.getEnderChestInventory().getStack(i));
            if (!el.isJsonNull()) ender.add(el);
        }
        root.add("ender_chest", ender);

        return reply(ctx, root);
    }

    private static JsonElement stackJson(ItemStack stack) {
        if (stack == null || stack.isEmpty()) return com.google.gson.JsonNull.INSTANCE;
        JsonObject o = new JsonObject();
        Identifier id = Registries.ITEM.getId(stack.getItem());
        o.addProperty("id", id == null ? "?" : id.toString());
        o.addProperty("count", stack.getCount());
        if (stack.hasCustomName()) {
            o.addProperty("name", stack.getName().getString());
        }
        var ench = EnchantmentHelper.get(stack);
        if (!ench.isEmpty()) {
            JsonArray arr = new JsonArray();
            ench.forEach((e, level) -> arr.add(e.getName(level).getString()));
            o.add("enchants", arr);
        }
        return o;
    }

    // ---------- xp -----------------------------------------------------------
    /**
     * { "player":"X", "level":27, "progress":0.43, "next_level_xp":67,
     *   "total_xp_collected":1234 }
     * progress is the 0..1 bar; next_level_xp is how many more XP points are
     * needed to hit level+1 (vanilla formula).
     */
    private static int queryXp(CommandContext<ServerCommandSource> ctx) {
        ServerPlayerEntity p = onlinePlayer(ctx);
        if (p == null) return 0;
        int level = p.experienceLevel;
        float progress = p.experienceProgress;
        int needed = p.getNextLevelExperience();
        int remaining = (int) Math.ceil(needed * (1.0f - progress));

        JsonObject o = new JsonObject();
        o.addProperty("player", p.getName().getString());
        o.addProperty("level", level);
        o.addProperty("progress", progress);
        o.addProperty("next_level_xp", remaining);
        o.addProperty("total_xp_collected", p.totalExperience);
        return reply(ctx, o);
    }

    // ---------- stats --------------------------------------------------------
    /**
     * Reads the saved stats file at world/stats/<uuid>.json. Sub-objects:
     *   minecraft:mined, minecraft:crafted, minecraft:used, minecraft:broken,
     *   minecraft:picked_up, minecraft:dropped, minecraft:killed,
     *   minecraft:killed_by, minecraft:custom
     *
     * Optional `type` arg trims to one section (e.g. "killed" or "custom").
     * Returns the top-N entries per section by count desc.
     *
     * Limitation: vanilla saves stats every ~5 minutes for online players,
     * so very recent activity may not be reflected. We force a flush via
     * ServerStatHandler.save() before reading.
     */
    private static int queryStats(CommandContext<ServerCommandSource> ctx, String typeFilter) {
        String name = StringArgumentType.getString(ctx, "player");
        MinecraftServer server = ctx.getSource().getServer();

        // Flush online player's stats so the file we're about to read is fresh.
        ServerPlayerEntity online = server.getPlayerManager().getPlayer(name);
        if (online != null) online.getStatHandler().save();

        // Resolve UUID — prefer online player; fall back to UserCache for offline.
        java.util.UUID uuid = online != null ? online.getUuid()
            : server.getUserCache() == null
                ? null
                : server.getUserCache().findByName(name).map(p -> p.getId()).orElse(null);
        if (uuid == null) {
            return error(ctx, "unknown player: " + name);
        }

        Path statsFile = server.getSavePath(WorldSavePath.STATS).resolve(uuid + ".json");
        if (!Files.exists(statsFile)) {
            return reply(ctx, emptyStats(name));
        }

        JsonObject parsed;
        try {
            parsed = GSON.fromJson(Files.readString(statsFile), JsonObject.class);
        } catch (IOException e) {
            return error(ctx, "stats file unreadable: " + e.getMessage());
        }

        JsonObject stats = parsed.has("stats") && parsed.get("stats").isJsonObject()
            ? parsed.getAsJsonObject("stats") : new JsonObject();

        JsonObject out = new JsonObject();
        out.addProperty("player", name);

        if (typeFilter != null) {
            String key = typeFilter.contains(":") ? typeFilter : ("minecraft:" + typeFilter);
            if (stats.has(key)) {
                out.add(key, topN(stats.getAsJsonObject(key), STATS_TOP_N));
            } else {
                out.addProperty("error", "no such stat type: " + key);
            }
        } else {
            // Return everything but cap each section to STATS_TOP_N.
            for (String key : stats.keySet()) {
                JsonElement el = stats.get(key);
                if (el.isJsonObject()) out.add(key, topN(el.getAsJsonObject(), STATS_TOP_N));
            }
        }
        return reply(ctx, out);
    }

    private static JsonObject emptyStats(String name) {
        JsonObject o = new JsonObject();
        o.addProperty("player", name);
        o.addProperty("note", "no stats file yet (player has not played, or has not been saved)");
        return o;
    }

    private static JsonObject topN(JsonObject section, int n) {
        // Sort by long value desc, take top n.
        record E(String k, long v) {}
        List<E> entries = new ArrayList<>(section.size());
        for (String k : section.keySet()) {
            try { entries.add(new E(k, section.get(k).getAsLong())); }
            catch (Exception ignored) {}
        }
        entries.sort(Comparator.<E>comparingLong(e -> e.v).reversed());
        JsonObject out = new JsonObject();
        int taken = 0;
        for (E e : entries) {
            if (taken++ >= n) break;
            out.addProperty(e.k, e.v);
        }
        return out;
    }

    // ---------- recipes ------------------------------------------------------
    /**
     * makes=true:  recipes whose output is the given item
     * makes=false: recipes that consume the given item as an ingredient
     *
     * Response: { "item":"...", "recipes":[ {recipe_json}, ... ] }
     * Each recipe entry has a stable shape; modded recipe types fall back to
     * a generic { "id", "type", "ingredients", "output" } summary.
     */
    private static int queryRecipes(CommandContext<ServerCommandSource> ctx, boolean makes) {
        String raw = StringArgumentType.getString(ctx, "item").trim();
        Identifier id = Identifier.tryParse(raw.contains(":") ? raw : ("minecraft:" + raw));
        if (id == null) return error(ctx, "bad item id: " + raw);
        var item = Registries.ITEM.containsId(id) ? Registries.ITEM.get(id) : null;
        if (item == null) return error(ctx, "unknown item: " + id);

        MinecraftServer server = ctx.getSource().getServer();
        var registryManager = server.getRegistryManager();

        JsonArray hits = new JsonArray();
        int count = 0;
        for (Recipe<?> r : server.getRecipeManager().values()) {
            boolean match;
            if (makes) {
                ItemStack out = r.getOutput(registryManager);
                match = out != null && out.getItem() == item;
            } else {
                match = false;
                for (Ingredient ing : r.getIngredients()) {
                    for (ItemStack s : ing.getMatchingStacks()) {
                        if (s.getItem() == item) { match = true; break; }
                    }
                    if (match) break;
                }
            }
            if (!match) continue;
            hits.add(recipeJson(r, registryManager));
            if (++count >= RECIPE_RESULT_CAP) break;
        }

        JsonObject out = new JsonObject();
        out.addProperty("item", id.toString());
        out.addProperty("direction", makes ? "produces" : "consumes");
        out.addProperty("count", hits.size());
        out.add("recipes", hits);
        return reply(ctx, out);
    }

    private static JsonObject recipeJson(Recipe<?> r,
                                         net.minecraft.registry.DynamicRegistryManager registryManager) {
        JsonObject o = new JsonObject();
        o.addProperty("id", r.getId().toString());
        Identifier typeId = Registries.RECIPE_TYPE.getId(r.getType());
        o.addProperty("type", typeId == null ? "?" : typeId.toString());

        ItemStack output = r.getOutput(registryManager);
        if (output != null && !output.isEmpty()) {
            o.add("output", stackJson(output));
        }

        if (r instanceof ShapedRecipe sh) {
            // Pattern + key map are vanilla-shaped-only; flatten ingredients into a 2D pattern array.
            JsonArray rows = new JsonArray();
            int w = sh.getWidth(), h = sh.getHeight();
            var ings = sh.getIngredients();
            for (int y = 0; y < h; y++) {
                JsonArray row = new JsonArray();
                for (int x = 0; x < w; x++) {
                    int idx = y * w + x;
                    Ingredient ing = idx < ings.size() ? ings.get(idx) : Ingredient.EMPTY;
                    row.add(ingredientJson(ing));
                }
                rows.add(row);
            }
            o.add("pattern", rows);
        } else {
            // Shapeless / smelting / modded — flat list of ingredient sets.
            JsonArray ings = new JsonArray();
            for (Ingredient ing : r.getIngredients()) {
                ings.add(ingredientJson(ing));
            }
            o.add("ingredients", ings);
            if (r instanceof AbstractCookingRecipe cook) {
                o.addProperty("cook_time", cook.getCookTime());
                o.addProperty("xp", cook.getExperience());
            }
        }
        return o;
    }

    private static JsonElement ingredientJson(Ingredient ing) {
        if (ing == null || ing.isEmpty()) return com.google.gson.JsonNull.INSTANCE;
        JsonArray arr = new JsonArray();
        for (ItemStack s : ing.getMatchingStacks()) {
            Identifier sid = Registries.ITEM.getId(s.getItem());
            arr.add(sid == null ? "?" : sid.toString());
        }
        return arr;
    }

    // ---------- trinkets -----------------------------------------------------
    /**
     * Trinkets API: equipped items in extra slots beyond armor (chest, head,
     * face, ring, belt, gloves, etc., depending on the modpack). Only works
     * for online players. Returns:
     *   { "player":"X", "total_equipped":N,
     *     "groups":{ "<group>":{ "<slot>":[ {item}, ... ] } } }
     * `groups` is empty if the player has no trinket component or every slot
     * is empty.
     */
    private static int queryTrinkets(CommandContext<ServerCommandSource> ctx) {
        if (!FabricLoader.getInstance().isModLoaded("trinkets")) {
            return error(ctx, "trinkets mod not loaded");
        }
        ServerPlayerEntity p = onlinePlayer(ctx);
        if (p == null) return 0;

        JsonObject root = new JsonObject();
        root.addProperty("player", p.getName().getString());

        try {
            var tcOpt = dev.emi.trinkets.api.TrinketsApi.getTrinketComponent(p);
            if (tcOpt.isEmpty()) {
                root.addProperty("note", "no trinket component for this player");
                root.addProperty("total_equipped", 0);
                return reply(ctx, root);
            }
            var tc = tcOpt.get();
            JsonObject groups = new JsonObject();
            int totalEquipped = 0;

            for (var groupEntry : tc.getInventory().entrySet()) {
                JsonObject groupJson = new JsonObject();
                for (var slotEntry : groupEntry.getValue().entrySet()) {
                    var inv = slotEntry.getValue();
                    JsonArray slotItems = new JsonArray();
                    for (int i = 0; i < inv.size(); i++) {
                        ItemStack stack = inv.getStack(i);
                        JsonElement el = stackJson(stack);
                        if (!el.isJsonNull()) {
                            slotItems.add(el);
                            totalEquipped++;
                        }
                    }
                    if (slotItems.size() > 0) {
                        groupJson.add(slotEntry.getKey(), slotItems);
                    }
                }
                if (!groupJson.entrySet().isEmpty()) {
                    groups.add(groupEntry.getKey(), groupJson);
                }
            }
            root.add("groups", groups);
            root.addProperty("total_equipped", totalEquipped);
        } catch (Throwable t) {
            return error(ctx, "trinkets api call failed: " + t.toString());
        }
        return reply(ctx, root);
    }

    // ---------- ftb-quests ---------------------------------------------------
    /**
     * Read FTB Quests progress for a player. With no `search` arg, returns a
     * summary (counts + chapters). With a `search` arg, returns up to
     * QUEST_HIT_CAP quests whose title or description text contains the
     * needle (case-insensitive) — useful for "where might I find the lich
     * king?" style questions where the answer is in quest description text.
     *
     * Per-team progress: FTB Quests scopes progress per team; we look up the
     * player's team data via getNullableTeamData().
     */
    private static int queryQuest(CommandContext<ServerCommandSource> ctx, String search) {
        if (!FabricLoader.getInstance().isModLoaded("ftbquests")) {
            return error(ctx, "ftb-quests not loaded");
        }
        ServerPlayerEntity p = onlinePlayer(ctx);
        if (p == null) return 0;

        try {
            var sqf = dev.ftb.mods.ftbquests.quest.ServerQuestFile.INSTANCE;
            if (sqf == null) return error(ctx, "FTB Quests not initialized yet");
            var td = sqf.getNullableTeamData(p.getUuid());

            JsonObject root = new JsonObject();
            root.addProperty("player", p.getName().getString());
            if (td == null) {
                root.addProperty("note", "no team data for this player");
                return reply(ctx, root);
            }

            // FTB Quests' QuestFile exposes only callback-style iteration
            // (forAllQuests / forAllChapters) — no public stream/list of
            // all quests. We accumulate via the consumer.
            if (search == null || search.isBlank()) {
                // Summary mode.
                int[] counts = {0, 0, 0}; // total, completed, started
                JsonArray chapters = new JsonArray();
                for (var chapter : sqf.getAllChapters()) {
                    int[] chCounts = {0, 0}; // total, done
                    for (var q : chapter.getQuests()) {
                        chCounts[0]++;
                        if (td.isCompleted(q)) chCounts[1]++;
                    }
                    JsonObject c = new JsonObject();
                    c.addProperty("title", chapter.getTitle().getString());
                    c.addProperty("completed", chCounts[1]);
                    c.addProperty("total", chCounts[0]);
                    chapters.add(c);
                }
                sqf.forAllQuests(q -> {
                    counts[0]++;
                    if (td.isCompleted(q)) counts[1]++;
                    else if (td.isStarted(q)) counts[2]++;
                });
                root.addProperty("total_quests", counts[0]);
                root.addProperty("completed", counts[1]);
                root.addProperty("in_progress", counts[2]);
                root.addProperty("not_started", counts[0] - counts[1] - counts[2]);
                root.add("chapters", chapters);
                root.addProperty("hint", "pass a search arg (a word or phrase) to find quests by title/description, e.g. 'lich king'");
            } else {
                String needle = search.toLowerCase();
                JsonArray hits = new JsonArray();
                int[] counter = {0};
                sqf.forAllQuests(q -> {
                    if (counter[0] >= QUEST_HIT_CAP) return;
                    String title = q.getTitle().getString();
                    String desc = describeQuest(q);
                    String hay = (title + " | " + desc).toLowerCase();
                    if (!hay.contains(needle)) return;
                    JsonObject h = new JsonObject();
                    h.addProperty("title", title);
                    h.addProperty("description", truncate(desc, 600));
                    h.addProperty("completed", td.isCompleted(q));
                    h.addProperty("started", td.isStarted(q));
                    h.addProperty("chapter", q.getChapter() != null ? q.getChapter().getTitle().getString() : "?");
                    hits.add(h);
                    counter[0]++;
                });
                root.addProperty("query", search);
                root.addProperty("hits", hits.size());
                root.add("matches", hits);
            }
            return reply(ctx, root);
        } catch (Throwable t) {
            return error(ctx, "ftb-quests api call failed: " + t.toString());
        }
    }

    private static String describeQuest(dev.ftb.mods.ftbquests.quest.Quest q) {
        try {
            // getRawDescription returns List<String> (the underlying lines
            // with formatting codes). getDescription returns the rendered
            // Text components. Raw is enough — strip §-codes ourselves.
            var desc = q.getRawDescription();
            if (desc == null || desc.isEmpty()) return "";
            StringBuilder sb = new StringBuilder();
            for (String line : desc) sb.append(line).append(' ');
            return sb.toString().replaceAll("§.", "").trim();
        } catch (Throwable t) {
            return "";
        }
    }

    private static String truncate(String s, int max) {
        if (s == null) return "";
        s = s.replaceAll("\\s+", " ").trim();
        return s.length() <= max ? s : s.substring(0, max - 1) + "…";
    }

    // ---------- find (chest scan) -------------------------------------------
    /**
     * Scan loaded chunks in <dim> for containers holding <item> and return
     * up to FIND_RESULT_CAP locations. Iterates the view-distance halo
     * around every online player in the dimension — chunks outside loaded
     * memory aren't scanned (that would require touching the chunk files
     * on disk, which is out of scope for an in-game query). For a
     * "find iron in my base" question this is enough since the asking
     * player is in their base.
     *
     * Block entities scanned: anything that implements Inventory — chests,
     * barrels, shulker boxes, hoppers, dispensers, droppers, modded
     * containers (most of them). Double chests show up as two separate
     * block entities, which is fine.
     */
    private static int queryFind(CommandContext<ServerCommandSource> ctx) {
        String dimRaw = StringArgumentType.getString(ctx, "dim");
        String itemRaw = StringArgumentType.getString(ctx, "item").trim();

        if (!dimRaw.contains(":")) dimRaw = "minecraft:" + dimRaw;
        Identifier dimId = Identifier.tryParse(dimRaw);
        if (dimId == null) return error(ctx, "bad dim: " + dimRaw);

        Identifier itemId = Identifier.tryParse(itemRaw.contains(":") ? itemRaw : ("minecraft:" + itemRaw));
        if (itemId == null) return error(ctx, "bad item: " + itemRaw);
        if (!Registries.ITEM.containsId(itemId)) return error(ctx, "unknown item: " + itemId);
        Item target = Registries.ITEM.get(itemId);

        ServerWorld world = ctx.getSource().getServer().getWorld(
            RegistryKey.of(RegistryKeys.WORLD, dimId)
        );
        if (world == null) return error(ctx, "no world for dim: " + dimId);

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
        return reply(ctx, root);
    }

    private static int countItems(Inventory inv, Item target) {
        int count = 0;
        for (int i = 0; i < inv.size(); i++) {
            ItemStack stack = inv.getStack(i);
            if (stack.getItem() == target) count += stack.getCount();
        }
        return count;
    }

    // ---------- skills (puffish_skills) -------------------------------------
    /**
     * Per-player skill-tree state from Puffish Skills. Reports each
     * configured category with the player's level, experience progress,
     * skill points (total / spent / unspent), and how many skills they've
     * unlocked vs total available. Modpacks like Prominence II expose
     * their leveling system through one or more Puffish categories.
     */
    private static int querySkills(CommandContext<ServerCommandSource> ctx) {
        if (!FabricLoader.getInstance().isModLoaded("puffish_skills")) {
            return error(ctx, "puffish_skills not loaded");
        }
        ServerPlayerEntity p = onlinePlayer(ctx);
        if (p == null) return 0;

        JsonObject root = new JsonObject();
        root.addProperty("player", p.getName().getString());
        JsonArray categories = new JsonArray();

        try {
            net.puffish.skillsmod.api.SkillsAPI.streamCategories().forEach(cat -> {
                JsonObject c = new JsonObject();
                c.addProperty("id", cat.getId().toString());
                c.addProperty("unlocked_for_player", cat.isUnlocked(p));
                c.addProperty("skill_points_total", cat.getPointsTotal(p));
                c.addProperty("skill_points_spent", cat.getSpentPoints(p));
                c.addProperty("skill_points_left", cat.getPointsLeft(p));

                cat.getExperience().ifPresent(exp -> {
                    int level = exp.getLevel(p);
                    c.addProperty("level", level);
                    c.addProperty("current_level_xp", exp.getCurrent(p));
                    c.addProperty("xp_to_next_level", exp.getRequired(p, level));
                    c.addProperty("total_xp_collected", exp.getTotal(p));
                });

                long total = cat.streamSkills().count();
                long unlocked = cat.streamUnlockedSkills(p).count();
                c.addProperty("skills_unlocked", unlocked);
                c.addProperty("skills_total", total);
                categories.add(c);
            });
        } catch (Throwable t) {
            return error(ctx, "puffish_skills api call failed: " + t.toString());
        }

        root.add("categories", categories);
        return reply(ctx, root);
    }

    // ---------- nbt_keys (index for unknown mod data) ------------------------
    /**
     * Lists the top-level keys of a player's serialized NBT, plus one level
     * of nested keys for any compound subtree. No values — just the index
     * Claude needs to know what `data get entity <player> <path>` calls are
     * worth making. This is the on-ramp for mod-specific data the bridge
     * doesn't have a dedicated tool for (Prominence levels, item-level
     * NBT under SelectedItem.tag, modded skill systems, etc.).
     *
     * Output:
     *   { "player":"X",
     *     "keys":{
     *       "<key>": "<type>"                  for primitive/list/array,
     *       "<key>:compound": ["<sk>:<type>"]  for nested compounds
     *     } }
     */
    private static int queryNbtKeys(CommandContext<ServerCommandSource> ctx) {
        ServerPlayerEntity p = onlinePlayer(ctx);
        if (p == null) return 0;
        net.minecraft.nbt.NbtCompound nbt = new net.minecraft.nbt.NbtCompound();
        p.writeNbt(nbt);

        JsonObject root = new JsonObject();
        root.addProperty("player", p.getName().getString());
        JsonObject keys = new JsonObject();
        for (String k : nbt.getKeys()) {
            net.minecraft.nbt.NbtElement el = nbt.get(k);
            if (el instanceof net.minecraft.nbt.NbtCompound sub) {
                JsonArray subKeys = new JsonArray();
                for (String sk : sub.getKeys()) {
                    subKeys.add(sk + ":" + nbtTypeName(sub.get(sk)));
                }
                keys.add(k + ":compound", subKeys);
            } else {
                keys.addProperty(k, nbtTypeName(el));
            }
        }
        root.add("keys", keys);
        root.addProperty("hint", "use `data get entity " + p.getName().getString() + " <path>` to read a specific subtree");
        return reply(ctx, root);
    }

    private static String nbtTypeName(net.minecraft.nbt.NbtElement el) {
        if (el == null) return "null";
        return switch (el.getType()) {
            case 1 -> "byte";
            case 2 -> "short";
            case 3 -> "int";
            case 4 -> "long";
            case 5 -> "float";
            case 6 -> "double";
            case 7 -> "byte[]";
            case 8 -> "string";
            case 9 -> "list[" + ((net.minecraft.nbt.NbtList) el).size() + "]";
            case 10 -> "compound{" + ((net.minecraft.nbt.NbtCompound) el).getSize() + "}";
            case 11 -> "int[]";
            case 12 -> "long[]";
            default -> "?";
        };
    }

    // ---------- helpers ------------------------------------------------------
    private static ServerPlayerEntity onlinePlayer(CommandContext<ServerCommandSource> ctx) {
        String name = StringArgumentType.getString(ctx, "player");
        ServerPlayerEntity p = ctx.getSource().getServer().getPlayerManager().getPlayer(name);
        if (p == null) {
            error(ctx, "player not online: " + name);
            return null;
        }
        return p;
    }

    private static int reply(CommandContext<ServerCommandSource> ctx, JsonObject o) {
        String json = GSON.toJson(o);
        if (json.length() > MAX_RESPONSE_CHARS) {
            JsonObject trim = new JsonObject();
            trim.addProperty("error", "response truncated; was " + json.length() + " chars");
            trim.addProperty("hint", "narrow the query (e.g. add a stats type filter)");
            json = GSON.toJson(trim);
        }
        final String out = json;
        ctx.getSource().sendFeedback(() -> Text.literal(out), false);
        return 1;
    }

    private static int error(CommandContext<ServerCommandSource> ctx, String msg) {
        JsonObject o = new JsonObject();
        o.addProperty("error", msg);
        ctx.getSource().sendFeedback(() -> Text.literal(GSON.toJson(o)), false);
        return 0;
    }
}
