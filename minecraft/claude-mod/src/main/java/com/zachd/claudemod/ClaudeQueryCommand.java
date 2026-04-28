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
import net.fabricmc.loader.api.ModContainer;
import net.minecraft.block.entity.BlockEntity;
import net.minecraft.enchantment.EnchantmentHelper;
import net.minecraft.entity.EntityType;
import net.minecraft.entity.LivingEntity;
import net.minecraft.entity.EquipmentSlot;
import net.minecraft.entity.attribute.DefaultAttributeContainer;
import net.minecraft.entity.attribute.DefaultAttributeRegistry;
import net.minecraft.entity.attribute.EntityAttribute;
import net.minecraft.entity.attribute.EntityAttributeInstance;
import net.minecraft.entity.attribute.EntityAttributeModifier;
import net.minecraft.entity.attribute.EntityAttributes;
import net.minecraft.entity.effect.StatusEffectInstance;
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
import net.minecraft.registry.tag.TagKey;
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
    // RCON output cap. Minecraft's RCON spec allows packets up to 4096
    // bytes; we leave a small margin for protocol framing.
    private static final int MAX_RESPONSE_CHARS = 3900;
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
    // Cap on items returned per tag query (some tags have hundreds).
    private static final int TAG_RESULT_CAP = 60;
    // Cap on mods returned without a search filter (we have ~400 in this pack).
    private static final int MODS_RESULT_CAP = 60;

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
                        .then(CommandManager.literal("available")
                            .then(CommandManager.argument("player", StringArgumentType.word())
                                .executes(ctx -> queryAvailableQuests(ctx, null))
                                .then(CommandManager.argument("chapter", StringArgumentType.greedyString())
                                    .executes(ctx -> queryAvailableQuests(ctx,
                                        StringArgumentType.getString(ctx, "chapter"))))))
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ctx -> queryQuest(ctx, null))
                            .then(CommandManager.argument("search", StringArgumentType.greedyString())
                                .executes(ctx -> queryQuest(ctx, StringArgumentType.getString(ctx, "search"))))))
                    .then(CommandManager.literal("find")
                        .then(CommandManager.argument("dim",
                                net.minecraft.command.argument.IdentifierArgumentType.identifier())
                            .then(CommandManager.argument("item", StringArgumentType.greedyString())
                                .executes(ClaudeQueryCommand::queryFind))))
                    .then(CommandManager.literal("nbt_keys")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ctx -> queryNbtKeys(ctx, null))
                            .then(CommandManager.argument("path", StringArgumentType.greedyString())
                                .executes(ctx -> queryNbtKeys(ctx,
                                    StringArgumentType.getString(ctx, "path"))))))
                    .then(CommandManager.literal("skills")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ClaudeQueryCommand::querySkills)))
                    .then(CommandManager.literal("vitals")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ClaudeQueryCommand::queryVitals)))
                    .then(CommandManager.literal("item")
                        .then(CommandManager.argument("id", StringArgumentType.greedyString())
                            .executes(ClaudeQueryCommand::queryItem)))
                    .then(CommandManager.literal("tag")
                        .then(CommandManager.argument("tag", StringArgumentType.greedyString())
                            .executes(ClaudeQueryCommand::queryTag)))
                    .then(CommandManager.literal("mob")
                        .then(CommandManager.argument("id", StringArgumentType.greedyString())
                            .executes(ClaudeQueryCommand::queryMob)))
                    .then(CommandManager.literal("mods")
                        .executes(ctx -> queryMods(ctx, null))
                        .then(CommandManager.argument("search", StringArgumentType.greedyString())
                            .executes(ctx -> queryMods(ctx, StringArgumentType.getString(ctx, "search")))))
                    .then(CommandManager.literal("gear")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ClaudeQueryCommand::queryGear)))
                    .then(CommandManager.literal("backpack")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ClaudeQueryCommand::queryBackpack)))
                    .then(CommandManager.literal("spells")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ClaudeQueryCommand::querySpells)))
                    .then(CommandManager.literal("here")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ClaudeQueryCommand::queryHere)))
                    .then(CommandManager.literal("nearest")
                        .then(CommandManager.literal("biome")
                            .then(CommandManager.argument("player", StringArgumentType.word())
                                .then(CommandManager.argument("id", StringArgumentType.greedyString())
                                    .executes(ctx -> queryNearest(ctx, true)))))
                        .then(CommandManager.literal("structure")
                            .then(CommandManager.argument("player", StringArgumentType.word())
                                .then(CommandManager.argument("id", StringArgumentType.greedyString())
                                    .executes(ctx -> queryNearest(ctx, false))))))
                    .then(CommandManager.literal("nbt_keys")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ctx -> queryNbtKeys(ctx, null))
                            .then(CommandManager.argument("path", StringArgumentType.greedyString())
                                .executes(ctx -> queryNbtKeys(ctx,
                                    StringArgumentType.getString(ctx, "path")))))))
                .then(CommandManager.literal("home")
                    .then(CommandManager.argument("player", StringArgumentType.word())
                        .executes(ClaudeQueryCommand::homeCommand)))
                .then(CommandManager.literal("query")
                    .then(CommandManager.literal("perf")
                        .executes(ClaudeQueryCommand::queryPerf)))
                .then(CommandManager.literal("bossbar")
                    .then(CommandManager.literal("update")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .then(CommandManager.argument("text", StringArgumentType.greedyString())
                                .executes(ClaudeQueryCommand::bossbarUpdate))))
                    .then(CommandManager.literal("remove")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ClaudeQueryCommand::bossbarRemove))))
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
        // Tiered mod adds tier/durability metadata under tag.Tiered/durable.
        // This is the modpack's actual "item level" surface — strip the
        // tiered: namespace for cleaner output (e.g. "legendary_melee_3").
        if (stack.hasNbt()) {
            var nbt = stack.getNbt();
            if (nbt.contains("Tiered", 10)) {
                var tiered = nbt.getCompound("Tiered");
                if (tiered.contains("Tier", 8)) {
                    String tier = tiered.getString("Tier");
                    if (tier.startsWith("tiered:")) tier = tier.substring(7);
                    o.addProperty("tier", tier);
                }
            }
            if (nbt.contains("durable", 6)) {
                o.addProperty("durability_mod", nbt.getDouble("durable"));
            }
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
                    // Cast to QuestObjectBase to bypass the Movable interface
                    // dispatch — Movable.getTitle() is abstract, but
                    // QuestObjectBase has the concrete impl. Without the
                    // cast javac emits invokeinterface on Movable and the
                    // JVM throws AbstractMethodError.
                    c.addProperty("title",
                        ((dev.ftb.mods.ftbquests.quest.QuestObjectBase) chapter).getRawTitle());
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
                    // Casts dodge the Movable.getTitle() interface dispatch
                    // (see comment above). QuestObjectBase has the concrete
                    // impl Quest/Chapter inherit.
                    String title = ((dev.ftb.mods.ftbquests.quest.QuestObjectBase) q)
                        .getRawTitle();
                    if (title == null) title = "";
                    String desc = describeQuest(q);
                    String hay = (title + " | " + desc).toLowerCase();
                    if (!hay.contains(needle)) return;
                    JsonObject h = new JsonObject();
                    h.addProperty("title", title);
                    h.addProperty("description", truncate(desc, 600));
                    h.addProperty("completed", td.isCompleted(q));
                    h.addProperty("started", td.isStarted(q));
                    var chapter = q.getChapter();
                    h.addProperty("chapter", chapter != null
                        ? ((dev.ftb.mods.ftbquests.quest.QuestObjectBase) chapter).getRawTitle()
                        : "?");
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

    /**
     * Quests the player can START RIGHT NOW: not completed, dependencies
     * satisfied, and visible to their team. Optional chapter filter is a
     * substring match on chapter title — the right path for "what
     * Getting Started quest can I do next?". Caps at 10 results, sorted
     * by chapter then title-as-discovered.
     */
    private static int queryAvailableQuests(CommandContext<ServerCommandSource> ctx, String chapterFilter) {
        if (!FabricLoader.getInstance().isModLoaded("ftbquests")) {
            return error(ctx, "ftb-quests not loaded");
        }
        ServerPlayerEntity p = onlinePlayer(ctx);
        if (p == null) return 0;

        try {
            var sqf = dev.ftb.mods.ftbquests.quest.ServerQuestFile.INSTANCE;
            if (sqf == null) return error(ctx, "FTB Quests not initialized yet");
            var td = sqf.getNullableTeamData(p.getUuid());
            if (td == null) {
                JsonObject root = new JsonObject();
                root.addProperty("player", p.getName().getString());
                root.addProperty("note", "no team data for this player");
                return reply(ctx, root);
            }

            String needle = chapterFilter == null ? null : chapterFilter.toLowerCase().trim();
            JsonArray hits = new JsonArray();
            int[] counter = {0};
            sqf.forAllQuests(q -> {
                if (counter[0] >= 10) return;
                if (td.isCompleted(q)) return;
                if (!q.areDependenciesComplete(td)) return;
                if (!q.isVisible(td)) return;

                var chapter = q.getChapter();
                String chTitle = chapter != null
                    ? ((dev.ftb.mods.ftbquests.quest.QuestObjectBase) chapter).getRawTitle()
                    : "";
                if (chTitle == null) chTitle = "";
                if (needle != null && !chTitle.toLowerCase().contains(needle)) return;

                String title = ((dev.ftb.mods.ftbquests.quest.QuestObjectBase) q).getRawTitle();
                if (title == null) title = "";

                JsonObject h = new JsonObject();
                h.addProperty("title", title);
                h.addProperty("chapter", chTitle);
                h.addProperty("description", truncate(describeQuest(q), 400));
                h.addProperty("started", td.isStarted(q));
                h.addProperty("optional", q.isOptional());
                hits.add(h);
                counter[0]++;
            });

            JsonObject root = new JsonObject();
            root.addProperty("player", p.getName().getString());
            if (chapterFilter != null) root.addProperty("chapter_filter", chapterFilter);
            root.addProperty("count", hits.size());
            root.add("quests", hits);
            if (hits.size() == 10) {
                root.addProperty("note", "showing first 10 — narrow with a chapter substring filter (e.g. \"Getting Started\")");
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
        Identifier dimId = net.minecraft.command.argument.IdentifierArgumentType
            .getIdentifier(ctx, "dim");
        String itemRaw = StringArgumentType.getString(ctx, "item").trim();

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
     * Per-player skill-tree state from Puffish Skills. SimplySkills (the
     * Prominence II class system) is built ON Puffish — its specializations
     * ARE puffish categories, and ability triggers fire from puffish skill
     * unlocks. So this single query covers BOTH puffish and SimplySkills.
     * More RPG Classes adds content (items/spells/effects) but no per-player
     * progression, so it has nothing to surface here.
     *
     * Compact JSON keys to fit RCON's 4kb cap on heavily-leveled players:
     *   id, lvl, xp, xp_next, sp_left, sp_spent, unlocked / total skills.
     */
    private static int querySkills(CommandContext<ServerCommandSource> ctx) {
        if (!FabricLoader.getInstance().isModLoaded("puffish_skills")) {
            return error(ctx, "puffish_skills not loaded");
        }
        ServerPlayerEntity p = onlinePlayer(ctx);
        if (p == null) return 0;

        JsonObject root = new JsonObject();
        root.addProperty("player", p.getName().getString());
        root.addProperty("note", "SimplySkills classes ARE these puffish categories; same data, no separate query needed");
        JsonArray categories = new JsonArray();

        try {
            net.puffish.skillsmod.api.SkillsAPI.streamCategories().forEach(cat -> {
                JsonObject c = new JsonObject();
                c.addProperty("id", cat.getId().toString());
                cat.getExperience().ifPresent(exp -> {
                    int level = exp.getLevel(p);
                    c.addProperty("lvl", level);
                    c.addProperty("xp", exp.getCurrent(p));
                    c.addProperty("xp_next", exp.getRequired(p, level));
                });
                c.addProperty("sp_left", cat.getPointsLeft(p));
                c.addProperty("sp_spent", cat.getSpentPoints(p));
                long total = cat.streamSkills().count();
                long unlocked = cat.streamUnlockedSkills(p).count();
                c.addProperty("unlocked", unlocked);
                c.addProperty("total", total);
                categories.add(c);
            });
        } catch (Throwable t) {
            return error(ctx, "puffish_skills api call failed: " + t.toString());
        }

        root.add("categories", categories);
        return reply(ctx, root);
    }

    // ---------- vitals -------------------------------------------------------
    /**
     * Live player vitals: health, hunger, air, status effects (with duration
     * + amplifier), and the resolved values of common attributes including
     * any modifiers from gear, affixes, skills, etc. This is what Claude
     * needs to answer "what does my held sword actually hit for?", "what
     * buffs am I running?", "why am I slow?".
     */
    private static int queryVitals(CommandContext<ServerCommandSource> ctx) {
        ServerPlayerEntity p = onlinePlayer(ctx);
        if (p == null) return 0;

        JsonObject root = new JsonObject();
        root.addProperty("player", p.getName().getString());
        root.addProperty("health", p.getHealth());
        root.addProperty("max_health", p.getMaxHealth());
        root.addProperty("hunger", p.getHungerManager().getFoodLevel());
        root.addProperty("saturation", p.getHungerManager().getSaturationLevel());
        root.addProperty("air", p.getAir());
        root.addProperty("max_air", p.getMaxAir());
        root.addProperty("xp_level", p.experienceLevel);

        // Active status effects.
        JsonArray effects = new JsonArray();
        for (StatusEffectInstance eff : p.getStatusEffects()) {
            JsonObject e = new JsonObject();
            Identifier eid = Registries.STATUS_EFFECT.getId(eff.getEffectType());
            e.addProperty("id", eid == null ? "?" : eid.toString());
            e.addProperty("name", eff.getEffectType().getName().getString());
            e.addProperty("amplifier", eff.getAmplifier());
            int dur = eff.getDuration();
            e.addProperty("duration_ticks", dur);
            // Vanilla "infinite" duration is -1; otherwise convert to seconds.
            if (dur >= 0) e.addProperty("duration_seconds", dur / 20);
            effects.add(e);
        }
        root.add("effects", effects);

        // Bed/respawn point. null when player has never slept in a bed.
        BlockPos spawn = p.getSpawnPointPosition();
        if (spawn != null) {
            JsonObject bed = new JsonObject();
            bed.addProperty("x", spawn.getX());
            bed.addProperty("y", spawn.getY());
            bed.addProperty("z", spawn.getZ());
            bed.addProperty("dim", p.getSpawnPointDimension().getValue().toString());
            bed.addProperty("angle", p.getSpawnAngle());
            root.add("bed_spawn", bed);
        }

        // Last death location, when set (gamerule keepInventory false +
        // recent death). Vanilla clears this on respawn so it tends to be
        // current-life-only.
        p.getLastDeathPos().ifPresent(global -> {
            JsonObject d = new JsonObject();
            d.addProperty("x", global.getPos().getX());
            d.addProperty("y", global.getPos().getY());
            d.addProperty("z", global.getPos().getZ());
            d.addProperty("dim", global.getDimension().getValue().toString());
            root.add("last_death", d);
        });

        // Resolved attribute values — base + all modifiers stacked.
        JsonObject attrs = new JsonObject();
        // 1.20.1 generic attributes only — the PLAYER_*_INTERACTION_RANGE
        // and BLOCK_BREAK_SPEED constants were added in 1.20.5+.
        var attributes = new net.minecraft.entity.attribute.EntityAttribute[] {
            EntityAttributes.GENERIC_MAX_HEALTH,
            EntityAttributes.GENERIC_ATTACK_DAMAGE,
            EntityAttributes.GENERIC_ATTACK_SPEED,
            EntityAttributes.GENERIC_ATTACK_KNOCKBACK,
            EntityAttributes.GENERIC_ARMOR,
            EntityAttributes.GENERIC_ARMOR_TOUGHNESS,
            EntityAttributes.GENERIC_KNOCKBACK_RESISTANCE,
            EntityAttributes.GENERIC_MOVEMENT_SPEED,
            EntityAttributes.GENERIC_LUCK,
        };
        for (var attr : attributes) {
            EntityAttributeInstance inst = p.getAttributeInstance(attr);
            if (inst == null) continue;
            Identifier aid = Registries.ATTRIBUTE.getId(attr);
            JsonObject a = new JsonObject();
            a.addProperty("base", inst.getBaseValue());
            a.addProperty("value", inst.getValue());
            attrs.add(aid == null ? "?" : aid.toString(), a);
        }
        root.add("attributes", attrs);
        return reply(ctx, root);
    }

    // ---------- item lookup --------------------------------------------------
    /**
     * Translate an item id into a friendlier description: display name,
     * which tags it belongs to (tags drive cross-mod equivalence — e.g.
     * #c:ingots/iron is what unifies modded iron variants), max stack size,
     * rarity, and the source mod. The bridge can hand item ids from
     * inventory dumps to this for nicer chat replies.
     */
    private static int queryItem(CommandContext<ServerCommandSource> ctx) {
        String raw = StringArgumentType.getString(ctx, "id").trim();
        Identifier id = Identifier.tryParse(raw.contains(":") ? raw : ("minecraft:" + raw));
        if (id == null) return error(ctx, "bad item id: " + raw);
        if (!Registries.ITEM.containsId(id)) return error(ctx, "unknown item: " + id);

        Item item = Registries.ITEM.get(id);
        ItemStack stack = item.getDefaultStack();

        JsonObject out = new JsonObject();
        out.addProperty("id", id.toString());
        out.addProperty("name", stack.getName().getString());
        out.addProperty("max_stack_size", item.getMaxCount());
        out.addProperty("rarity", stack.getRarity().name().toLowerCase());
        out.addProperty("mod_id", id.getNamespace());

        // Tags this item belongs to. Iterating Registries.ITEM.streamTags()
        // is server-cheap because tags are a small flat structure.
        JsonArray tags = new JsonArray();
        Registries.ITEM.streamTags()
            .filter(tag -> Registries.ITEM.getEntryList(tag)
                .map(list -> list.stream().anyMatch(e -> e.value() == item))
                .orElse(false))
            .forEach(tag -> tags.add("#" + tag.id().toString()));
        out.add("tags", tags);
        return reply(ctx, out);
    }

    // ---------- tag membership ----------------------------------------------
    /**
     * List items in an item tag (cross-mod equivalence groups). Player can
     * include or omit the leading '#'; we accept either. Caps at
     * TAG_RESULT_CAP entries — modded packs have very large tags.
     */
    private static int queryTag(CommandContext<ServerCommandSource> ctx) {
        String raw = StringArgumentType.getString(ctx, "tag").trim();
        if (raw.startsWith("#")) raw = raw.substring(1);
        Identifier tagId = Identifier.tryParse(raw.contains(":") ? raw : ("minecraft:" + raw));
        if (tagId == null) return error(ctx, "bad tag: " + raw);

        TagKey<Item> key = TagKey.of(RegistryKeys.ITEM, tagId);
        var entryListOpt = Registries.ITEM.getEntryList(key);
        if (entryListOpt.isEmpty()) {
            JsonObject empty = new JsonObject();
            empty.addProperty("tag", "#" + tagId);
            empty.addProperty("hits", 0);
            empty.add("items", new JsonArray());
            empty.addProperty("note", "no such tag, or tag is empty");
            return reply(ctx, empty);
        }
        JsonArray items = new JsonArray();
        int total = 0;
        boolean truncated = false;
        for (var entry : entryListOpt.get()) {
            if (items.size() >= TAG_RESULT_CAP) { truncated = true; break; }
            Identifier iid = Registries.ITEM.getId(entry.value());
            if (iid != null) items.add(iid.toString());
            total++;
        }
        // Count the rest without adding to the array, just so the caller
        // knows there's more.
        if (truncated) {
            for (var ignored : entryListOpt.get()) total++;
        }
        JsonObject out = new JsonObject();
        out.addProperty("tag", "#" + tagId);
        out.addProperty("hits", entryListOpt.get().size());
        out.add("items", items);
        if (truncated) out.addProperty("truncated", true);
        return reply(ctx, out);
    }

    // ---------- mob info -----------------------------------------------------
    /**
     * Static info about an entity type (vanilla or modded): display name,
     * source mod, default attribute values (max HP, attack damage, armor
     * if applicable), and the loot table id. We can't get exact XP drops
     * without instantiating, so we leave that to Claude / WebSearch — the
     * loot table id is enough to look up more if needed.
     */
    private static int queryMob(CommandContext<ServerCommandSource> ctx) {
        String raw = StringArgumentType.getString(ctx, "id").trim();
        Identifier id = Identifier.tryParse(raw.contains(":") ? raw : ("minecraft:" + raw));
        if (id == null) return error(ctx, "bad entity id: " + raw);
        if (!Registries.ENTITY_TYPE.containsId(id)) return error(ctx, "unknown entity: " + id);

        EntityType<?> type = Registries.ENTITY_TYPE.get(id);
        JsonObject out = new JsonObject();
        out.addProperty("id", id.toString());
        out.addProperty("name", type.getName().getString());
        out.addProperty("category", type.getSpawnGroup().getName());
        out.addProperty("mod_id", id.getNamespace());
        out.addProperty("loot_table", type.getLootTableId().toString());

        // Pull the default attribute container if this is a LivingEntity type.
        try {
            @SuppressWarnings("unchecked")
            EntityType<? extends LivingEntity> living = (EntityType<? extends LivingEntity>) type;
            DefaultAttributeContainer attrs = DefaultAttributeRegistry.get(living);
            if (attrs != null) {
                JsonObject a = new JsonObject();
                var keys = new net.minecraft.entity.attribute.EntityAttribute[] {
                    EntityAttributes.GENERIC_MAX_HEALTH,
                    EntityAttributes.GENERIC_ATTACK_DAMAGE,
                    EntityAttributes.GENERIC_ARMOR,
                    EntityAttributes.GENERIC_MOVEMENT_SPEED,
                    EntityAttributes.GENERIC_FOLLOW_RANGE,
                    EntityAttributes.GENERIC_KNOCKBACK_RESISTANCE,
                };
                for (var k : keys) {
                    if (attrs.has(k)) {
                        Identifier aid = Registries.ATTRIBUTE.getId(k);
                        a.addProperty(aid == null ? "?" : aid.toString(),
                                      attrs.getBaseValue(k));
                    }
                }
                out.add("attributes", a);
            }
        } catch (ClassCastException ignored) {
            // Not a LivingEntity (e.g. arrow, item, boat) — no attributes.
        } catch (Throwable t) {
            ClaudeMod.LOG.warn("mob query attribute lookup failed: {}", t.toString());
        }
        return reply(ctx, out);
    }

    // ---------- mods ---------------------------------------------------------
    /**
     * List loaded Fabric mods. Without a search arg, returns the count plus
     * the first MODS_RESULT_CAP sorted by id. With a search arg, returns
     * mods whose id, name, or description (case-insensitive substring) match.
     * Useful for "what mods give wings?", "which one adds the dragon eggs?",
     * and as a self-introspection tool when Claude needs to know what's
     * actually installed.
     */
    private static int queryMods(CommandContext<ServerCommandSource> ctx, String search) {
        var loader = FabricLoader.getInstance();
        var allMods = new ArrayList<>(loader.getAllMods());
        allMods.sort(Comparator.comparing(m -> m.getMetadata().getId()));

        JsonObject root = new JsonObject();
        root.addProperty("total_loaded", allMods.size());

        JsonArray arr = new JsonArray();
        boolean truncated = false;
        if (search == null || search.isBlank()) {
            for (ModContainer m : allMods) {
                if (arr.size() >= MODS_RESULT_CAP) { truncated = true; break; }
                arr.add(modJson(m));
            }
            if (truncated) {
                root.addProperty("truncated", true);
                root.addProperty("hint", "pass a search term to filter, e.g. `claudemod query mods spell`");
            }
        } else {
            String needle = search.toLowerCase();
            for (ModContainer m : allMods) {
                var md = m.getMetadata();
                String hay = (md.getId() + " " + md.getName() + " " + md.getDescription()).toLowerCase();
                if (!hay.contains(needle)) continue;
                if (arr.size() >= MODS_RESULT_CAP) { truncated = true; break; }
                arr.add(modJson(m));
            }
            root.addProperty("query", search);
            if (truncated) root.addProperty("truncated", true);
        }
        root.addProperty("returned", arr.size());
        root.add("mods", arr);
        return reply(ctx, root);
    }

    private static JsonObject modJson(ModContainer m) {
        var md = m.getMetadata();
        JsonObject o = new JsonObject();
        o.addProperty("id", md.getId());
        o.addProperty("name", md.getName());
        o.addProperty("version", md.getVersion().getFriendlyString());
        String desc = md.getDescription();
        if (desc != null && !desc.isEmpty()) {
            o.addProperty("description", truncate(desc, 200));
        }
        return o;
    }

    // ---------- gear (resolved item attributes per slot) ---------------------
    /**
     * Per-equipment-slot breakdown of which item contributes which
     * attribute modifier — answers "is this new sword better?", "where's
     * my crit chance coming from?", "which armor piece carries my speed?".
     *
     * For each of main_hand, off_hand, head/chest/legs/feet, walks the
     * stack's getAttributeModifiers(slot) — that's the vanilla API path
     * and Apotheosis/Zenith/Custom Item Attributes plug their affix
     * modifiers into the same map, so we get them for free.
     *
     * Plus a `summary` block with per-attribute totals (sum of all
     * additions across slots) so Claude can answer "what's my total attack
     * damage from gear?" without summing it itself. The fully resolved
     * value (base + all modifiers including non-gear sources) is in
     * `claudemod query vitals` — `gear` is for breakdown by source.
     */
    private static int queryGear(CommandContext<ServerCommandSource> ctx) {
        ServerPlayerEntity p = onlinePlayer(ctx);
        if (p == null) return 0;

        JsonObject root = new JsonObject();
        root.addProperty("player", p.getName().getString());

        // Tracking: per-attribute sum across all slots, addition-only (the
        // common case; multiplicative modifiers are reported per slot but
        // not summed here since they don't combine linearly).
        java.util.Map<Identifier, Double> additionSums = new java.util.LinkedHashMap<>();

        JsonObject slots = new JsonObject();
        var slotMap = new java.util.LinkedHashMap<String, EquipmentSlot>();
        slotMap.put("main_hand", EquipmentSlot.MAINHAND);
        slotMap.put("off_hand", EquipmentSlot.OFFHAND);
        slotMap.put("head", EquipmentSlot.HEAD);
        slotMap.put("chest", EquipmentSlot.CHEST);
        slotMap.put("legs", EquipmentSlot.LEGS);
        slotMap.put("feet", EquipmentSlot.FEET);

        for (var e : slotMap.entrySet()) {
            ItemStack stack = p.getEquippedStack(e.getValue());
            JsonObject sj = new JsonObject();
            if (stack == null || stack.isEmpty()) {
                sj.addProperty("empty", true);
                slots.add(e.getKey(), sj);
                continue;
            }
            Identifier iid = Registries.ITEM.getId(stack.getItem());
            sj.addProperty("id", iid == null ? "?" : iid.toString());
            sj.addProperty("name", stack.getName().getString());
            sj.addProperty("count", stack.getCount());

            JsonArray mods = new JsonArray();
            try {
                var multimap = stack.getAttributeModifiers(e.getValue());
                for (var entry : multimap.entries()) {
                    EntityAttribute attr = entry.getKey();
                    EntityAttributeModifier mod = entry.getValue();
                    Identifier aid = Registries.ATTRIBUTE.getId(attr);
                    if (aid == null) continue;
                    if (mod.getValue() == 0.0) continue;  // drop zero modifiers
                    JsonObject m = new JsonObject();
                    // Strip "minecraft:" prefix and abbreviate the operation
                    // to keep the response under the RCON cap on a heavily
                    // affixed player.
                    String shortAttr = aid.getNamespace().equals("minecraft")
                        ? aid.getPath() : aid.toString();
                    m.addProperty("a", shortAttr);
                    m.addProperty("v", mod.getValue());
                    m.addProperty("op", _shortOp(mod.getOperation()));
                    mods.add(m);
                    if (mod.getOperation() == EntityAttributeModifier.Operation.ADDITION) {
                        additionSums.merge(aid, mod.getValue(), Double::sum);
                    }
                }
            } catch (Throwable t) {
                ClaudeMod.LOG.warn("attr lookup failed for {}: {}",
                    iid, t.toString());
            }
            sj.add("mods", mods);
            slots.add(e.getKey(), sj);
        }
        root.add("slots", slots);

        JsonObject summary = new JsonObject();
        for (var entry : additionSums.entrySet()) {
            String shortKey = entry.getKey().getNamespace().equals("minecraft")
                ? entry.getKey().getPath() : entry.getKey().toString();
            summary.addProperty(shortKey, entry.getValue());
        }
        root.add("totals_added", summary);
        root.addProperty("legend", "op: +=add, ×b=multiply_base, ×t=multiply_total");
        return reply(ctx, root);
    }

    private static String _shortOp(EntityAttributeModifier.Operation op) {
        return switch (op) {
            case ADDITION -> "+";
            case MULTIPLY_BASE -> "×b";
            case MULTIPLY_TOTAL -> "×t";
        };
    }

    // ---------- backpack (Travelers Backpack contents) -----------------------
    /**
     * Surface the equipped Travelers Backpack's storage / tool slots /
     * upgrades. Empty slots are omitted to keep the response small.
     * Skipped if the mod isn't loaded or the player isn't wearing one.
     */
    private static int queryBackpack(CommandContext<ServerCommandSource> ctx) {
        if (!FabricLoader.getInstance().isModLoaded("travelersbackpack")) {
            return error(ctx, "travelersbackpack not loaded");
        }
        ServerPlayerEntity p = onlinePlayer(ctx);
        if (p == null) return 0;

        JsonObject root = new JsonObject();
        root.addProperty("player", p.getName().getString());

        try {
            if (!com.tiviacz.travelersbackpack.component.ComponentUtils.isWearingBackpack(p)) {
                root.addProperty("equipped", false);
                return reply(ctx, root);
            }
            ItemStack bpStack = com.tiviacz.travelersbackpack.component.ComponentUtils.getWearingBackpack(p);
            var wrapper = com.tiviacz.travelersbackpack.component.ComponentUtils.getBackpackWrapper(p);
            root.addProperty("equipped", true);
            Identifier bid = Registries.ITEM.getId(bpStack.getItem());
            root.addProperty("backpack_id", bid == null ? "?" : bid.toString());
            root.addProperty("backpack_name", bpStack.getName().getString());

            // storage / tools / upgrades — each is an ItemStackHandler with
            // getSlots() + getStackInSlot(int).
            root.add("storage", _dumpItemHandler(wrapper.getStorage()));
            root.add("tools",   _dumpItemHandler(wrapper.getTools()));
            root.add("upgrades",_dumpItemHandler(wrapper.getUpgrades()));
            root.addProperty("tank_capacity_mb", wrapper.getBackpackTankCapacity());
        } catch (Throwable t) {
            return error(ctx, "travelersbackpack api call failed: " + t.toString());
        }
        return reply(ctx, root);
    }

    /** Dump non-empty slots of an ItemStackHandler-like inventory to JSON. */
    private static JsonArray _dumpItemHandler(Object handler) {
        JsonArray arr = new JsonArray();
        if (handler == null) return arr;
        try {
            int slots = (int) handler.getClass().getMethod("getSlots").invoke(handler);
            var getStack = handler.getClass().getMethod("getStackInSlot", int.class);
            for (int i = 0; i < slots; i++) {
                ItemStack s = (ItemStack) getStack.invoke(handler, i);
                if (s == null || s.isEmpty()) continue;
                JsonElement el = stackJson(s);
                if (el != null && !el.isJsonNull()) {
                    JsonObject o = el.getAsJsonObject();
                    o.addProperty("slot", i);
                    arr.add(o);
                }
            }
        } catch (Throwable t) {
            ClaudeMod.LOG.warn("itemhandler dump failed: {}", t.toString());
        }
        return arr;
    }

    // ---------- spells (Spell Engine) ----------------------------------------
    /**
     * Best-effort surface of a player's spell state. Spell Engine 0.x has
     * no per-player "known spells" pool — spells come from items the
     * player has equipped (typically a SpellBookItem in a Trinkets slot).
     * We walk the trinket slots, find any item whose registered id namespace
     * contains "spell" (or whose item class is a SpellBookItem), and dump
     * the SpellContainer NBT plus the ItemStack's full NBT for Claude to
     * interpret. Plus we surface attribute values from spell_power's
     * attribute namespace if any are present.
     *
     * No "mana" — Spell Engine uses cooldowns, not a mana pool.
     */
    private static int querySpells(CommandContext<ServerCommandSource> ctx) {
        ServerPlayerEntity p = onlinePlayer(ctx);
        if (p == null) return 0;

        JsonObject root = new JsonObject();
        root.addProperty("player", p.getName().getString());
        root.addProperty("note", "spell_engine 0.x has no mana — spells use cooldowns; spell power scales damage");

        // Equipped held items: main + off hand
        JsonArray equipped = new JsonArray();
        for (EquipmentSlot slot : new EquipmentSlot[] { EquipmentSlot.MAINHAND, EquipmentSlot.OFFHAND }) {
            ItemStack stack = p.getEquippedStack(slot);
            if (stack == null || stack.isEmpty()) continue;
            Identifier id = Registries.ITEM.getId(stack.getItem());
            if (id == null) continue;
            if (_looksSpellRelated(stack, id)) {
                equipped.add(_spellItemJson(stack, id, slot.getName()));
            }
        }

        // Trinket slots (where most spellbooks live in this pack)
        if (FabricLoader.getInstance().isModLoaded("trinkets")) {
            try {
                var tcOpt = dev.emi.trinkets.api.TrinketsApi.getTrinketComponent(p);
                tcOpt.ifPresent(tc -> {
                    for (var groupEntry : tc.getInventory().entrySet()) {
                        for (var slotEntry : groupEntry.getValue().entrySet()) {
                            var inv = slotEntry.getValue();
                            for (int i = 0; i < inv.size(); i++) {
                                ItemStack stack = inv.getStack(i);
                                if (stack == null || stack.isEmpty()) continue;
                                Identifier id = Registries.ITEM.getId(stack.getItem());
                                if (id == null) continue;
                                if (_looksSpellRelated(stack, id)) {
                                    equipped.add(_spellItemJson(stack, id,
                                        "trinket:" + groupEntry.getKey() + "/" + slotEntry.getKey()));
                                }
                            }
                        }
                    }
                });
            } catch (Throwable t) {
                ClaudeMod.LOG.warn("trinkets walk for spells failed: {}", t.toString());
            }
        }
        root.add("spell_items", equipped);

        // Surface any spell_power-namespaced attributes the player has.
        JsonObject spellAttrs = new JsonObject();
        for (EntityAttribute attr : Registries.ATTRIBUTE) {
            Identifier aid = Registries.ATTRIBUTE.getId(attr);
            if (aid == null) continue;
            if (!aid.getNamespace().contains("spell")) continue;
            EntityAttributeInstance inst = p.getAttributeInstance(attr);
            if (inst == null) continue;
            JsonObject a = new JsonObject();
            a.addProperty("base", inst.getBaseValue());
            a.addProperty("value", inst.getValue());
            spellAttrs.add(aid.toString(), a);
        }
        root.add("spell_attributes", spellAttrs);
        return reply(ctx, root);
    }

    private static boolean _looksSpellRelated(ItemStack stack, Identifier id) {
        if (id.getNamespace().contains("spell")) return true;
        if (id.getPath().contains("spell")) return true;
        if (id.getPath().contains("grimoire")) return true;
        if (id.getPath().contains("tome")) return true;
        if (id.getPath().contains("staff")) return true;
        if (id.getPath().contains("wand")) return true;
        // Detect SpellBookItem class without hard-importing (so non-spell mods
        // don't break compilation if the API changes).
        try {
            Class<?> sbi = Class.forName("net.spell_engine.api.item.trinket.SpellBookItem");
            if (sbi.isInstance(stack.getItem())) return true;
        } catch (Throwable ignored) {}
        return false;
    }

    private static JsonObject _spellItemJson(ItemStack stack, Identifier id, String slotLabel) {
        JsonObject o = new JsonObject();
        o.addProperty("slot", slotLabel);
        o.addProperty("id", id.toString());
        o.addProperty("name", stack.getName().getString());
        if (stack.hasNbt()) {
            // Full NBT as SNBT — Claude can probe spell_engine-specific keys.
            o.addProperty("nbt_snbt", stack.getNbt().toString());
        }
        return o;
    }

    // ---------- here (current position context) -----------------------------
    /**
     * Ground-truth context about a player's CURRENT position: coords +
     * dimension + biome at that pos + light levels + the block at their
     * feet + whether the sky is visible + whether it's raining here.
     * Vanilla has no "what biome am I in?" command — `/locate biome` only
     * finds the nearest match, not the current one — so this fills the gap
     * for "where am I?", "what biome is this?", "is it raining here?",
     * "can I sleep here right now?" type questions.
     */
    private static int queryHere(CommandContext<ServerCommandSource> ctx) {
        ServerPlayerEntity p = onlinePlayer(ctx);
        if (p == null) return 0;
        ServerWorld world = (ServerWorld) p.getWorld();
        BlockPos pos = p.getBlockPos();

        JsonObject root = new JsonObject();
        root.addProperty("player", p.getName().getString());
        JsonObject coords = new JsonObject();
        coords.addProperty("x", pos.getX());
        coords.addProperty("y", pos.getY());
        coords.addProperty("z", pos.getZ());
        coords.addProperty("dim", world.getRegistryKey().getValue().toString());
        root.add("pos", coords);

        // Biome at the player's exact position.
        try {
            var biomeEntry = world.getBiome(pos);
            var key = biomeEntry.getKey();
            if (key.isPresent()) {
                root.addProperty("biome", key.get().getValue().toString());
            }
        } catch (Throwable t) {
            ClaudeMod.LOG.warn("biome lookup failed: {}", t.toString());
        }

        // Light levels — block (torches/glowstone) vs sky (sun/moon).
        JsonObject light = new JsonObject();
        light.addProperty("block",
            world.getLightLevel(net.minecraft.world.LightType.BLOCK, pos));
        light.addProperty("sky",
            world.getLightLevel(net.minecraft.world.LightType.SKY, pos));
        light.addProperty("total", world.getLightLevel(pos));
        root.add("light", light);
        root.addProperty("sees_sky", world.isSkyVisible(pos));

        // Block at feet + 1 above (head). Useful for "am I in water?",
        // "am I standing on netherrack?".
        Identifier feet = Registries.BLOCK.getId(world.getBlockState(pos).getBlock());
        Identifier head = Registries.BLOCK.getId(world.getBlockState(pos.up()).getBlock());
        root.addProperty("block_at_feet", feet == null ? "?" : feet.toString());
        root.addProperty("block_at_head", head == null ? "?" : head.toString());

        // Weather: per-position rain, biome precipitation, raining (global).
        JsonObject weather = new JsonObject();
        weather.addProperty("raining_global", world.isRaining());
        weather.addProperty("thundering_global", world.isThundering());
        weather.addProperty("raining_here", world.hasRain(pos));
        root.add("weather", weather);

        root.addProperty("time_of_day", (int)(world.getTimeOfDay() % 24000));
        return reply(ctx, root);
    }

    // ---------- nearest biome / structure ------------------------------------
    /**
     * Locate the nearest biome or structure to a player.
     *
     * Biome search: bounded sampling via ServerWorld.locateBiome. Doesn't
     * need to load chunks beyond the noise/biome layer, so this is fast
     * and watchdog-safe at 2048-block radius.
     *
     * Structure search: hybrid.
     *   1. Scan already-LOADED chunks within 32-chunk radius via
     *      WorldChunk.getStructureStart. Free / watchdog-safe — covers
     *      "where was that village I visited?".
     *   2. If nothing found, fall through to vanilla
     *      ChunkGenerator.locateStructure with a HARD-CAPPED 10-chunk
     *      radius. That's about 1-2 candidate spread points on a
     *      typical Random-Spread structure, so chunk loading stays
     *      small enough to fit under the watchdog budget.
     *
     * Earlier attempts that crashed:
     *   - 100 chunk radius → 60s+ chunk loads, watchdog killed JVM.
     *   - 25 chunk radius  → 51s chunk loads, vanilla's own timeout
     *     caught it but TPS dropped to ~1.8 for a minute.
     */
    private static final int BIOME_SEARCH_RADIUS_BLOCKS = 2048;
    private static final int LOADED_STRUCTURE_RADIUS_CHUNKS = 16;
    // Strict upper bound on the on-demand fallback when the loaded scan
    // misses. 5 chunks ≈ 80 blocks. Earlier 10-chunk attempts still cost
    // 52s of server-thread time on village searches in this pack (vanilla's
    // own timeout caught it, no watchdog kill, but TPS suffered). 5 keeps
    // the candidate spread set tiny — at most 0-1 points to verify.
    // Wider scans need to move off the server thread.
    private static final int ON_DEMAND_STRUCTURE_RADIUS_CHUNKS = 5;

    private static int queryNearest(CommandContext<ServerCommandSource> ctx, boolean isBiome) {
        String idStr = StringArgumentType.getString(ctx, "id").trim();
        Identifier id = Identifier.tryParse(idStr.contains(":") ? idStr : "minecraft:" + idStr);
        if (id == null) return error(ctx, "bad id: " + idStr);

        ServerPlayerEntity p = onlinePlayer(ctx);
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
                RegistryKey<net.minecraft.world.biome.Biome> bk = RegistryKey.of(RegistryKeys.BIOME, id);
                if (!biomeReg.contains(bk)) return error(ctx, "unknown biome: " + id);
                var pair = world.locateBiome(
                    entry -> entry.matchesKey(bk),
                    origin, BIOME_SEARCH_RADIUS_BLOCKS, 32, 64
                );
                found = pair == null ? null : pair.getFirst();
                radiusBlocks = BIOME_SEARCH_RADIUS_BLOCKS;
            } else {
                var structReg = world.getRegistryManager().get(RegistryKeys.STRUCTURE);
                RegistryKey<net.minecraft.world.gen.structure.Structure> sk =
                    RegistryKey.of(RegistryKeys.STRUCTURE, id);
                var entryOpt = structReg.getEntry(sk);
                if (entryOpt.isEmpty()) return error(ctx, "unknown structure: " + id);
                var target = entryOpt.get().value();
                // Step 1: scan already-loaded chunks (free, watchdog-safe).
                found = findLoadedStructure(world, origin, target);
                radiusBlocks = LOADED_STRUCTURE_RADIUS_CHUNKS * 16;
                // Step 2: if nothing in loaded chunks, try a tightly-bounded
                // on-demand vanilla locate. 10 chunks is small enough that
                // even with chunk loading the scan stays under a few
                // seconds in the typical case.
                if (found == null) {
                    var entryList = net.minecraft.registry.entry.RegistryEntryList.of(entryOpt.get());
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
            return error(ctx, "lookup failed: " + t.getClass().getSimpleName());
        }
        return reply(ctx, root);
    }

    /**
     * Walk the LOADED chunk halo around `origin` and return the closest
     * BlockPos that has a structure start matching `target`. No chunk
     * loading — only chunks currently held in memory by the server are
     * inspected. Returns null if no match is loaded.
     */
    private static BlockPos findLoadedStructure(ServerWorld world, BlockPos origin,
                                                 net.minecraft.world.gen.structure.Structure target) {
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

    // ---------- perf (server health) ----------------------------------------
    /**
     * Server perf snapshot: average MSPT (ms per tick) → derived TPS,
     * per-dimension loaded chunk counts, entity counts, online players,
     * and JVM heap usage. Answers "is the server lagging?", "what's eating
     * resources?". No external profiler needed — uses MinecraftServer's
     * built-in tick-time array and ServerWorld accessors.
     */
    private static int queryPerf(CommandContext<ServerCommandSource> ctx) {
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

        return reply(ctx, root);
    }

    // ---------- bossbar (silent — no log spam) ------------------------------
    /**
     * Bossbar manipulation that doesn't echo feedback to the server log.
     * The vanilla `bossbar` family routes through Brigadier and emits
     * "[Rcon: Set ... for custom bossbar X]" lines on every set, which
     * spams /data/logs because the bridge issues five of them per
     * progress update. We bypass that by talking to BossBarManager
     * directly and never calling sendFeedback.
     *
     * `update` is idempotent: creates the bossbar if absent, otherwise
     * updates name + ensures the player is attached. Hardcodes color,
     * max, and value to the values the bridge always wants.
     *
     * Bossbar id format mirrors the bridge's prior convention:
     *   claudemod:claude_<sanitized_player_name>
     */
    private static final net.minecraft.entity.boss.BossBar.Color BOSSBAR_COLOR =
        net.minecraft.entity.boss.BossBar.Color.BLUE;

    private static net.minecraft.util.Identifier _bossbarId(String player) {
        String safe = player.toLowerCase().replaceAll("[^a-z0-9_]", "_");
        return new net.minecraft.util.Identifier("claudemod", "claude_" + safe);
    }

    private static int bossbarUpdate(CommandContext<ServerCommandSource> ctx) {
        String playerName = StringArgumentType.getString(ctx, "player");
        String text = StringArgumentType.getString(ctx, "text");
        MinecraftServer server = ctx.getSource().getServer();
        ServerPlayerEntity p = server.getPlayerManager().getPlayer(playerName);
        if (p == null) return 0;

        net.minecraft.util.Identifier id = _bossbarId(playerName);
        var manager = server.getBossBarManager();
        net.minecraft.entity.boss.CommandBossBar bar = manager.get(id);
        if (bar == null) {
            bar = manager.add(id, net.minecraft.text.Text.literal(text));
            bar.setColor(BOSSBAR_COLOR);
            bar.setMaxValue(1);
            bar.setValue(1);
        } else {
            bar.setName(net.minecraft.text.Text.literal(text));
        }
        if (!bar.getPlayers().contains(p)) {
            bar.addPlayer(p);
        }
        return 1;
    }

    private static int bossbarRemove(CommandContext<ServerCommandSource> ctx) {
        String playerName = StringArgumentType.getString(ctx, "player");
        net.minecraft.util.Identifier id = _bossbarId(playerName);
        var manager = ctx.getSource().getServer().getBossBarManager();
        net.minecraft.entity.boss.CommandBossBar bar = manager.get(id);
        if (bar != null) {
            // clearPlayers sends the REMOVE packet to attached clients so
            // they actually hide the bar; manager.remove alone just drops
            // it from the registry without notifying anyone.
            bar.clearPlayers();
            manager.remove(bar);
        }
        return 1;
    }

    // ---------- home (teleport to bed/spawn) --------------------------------
    /**
     * Teleport a player to their bed/respawn point. RCON-only, intended to
     * be invoked by the bridge tool teleport_caller_home which substitutes
     * the asking player's name from CALLER_PLAYER. Returns a structured
     * error if the player has no spawn set (never slept in a bed).
     *
     * Uses ServerPlayerEntity.teleport which handles cross-dimension moves
     * cleanly — important because bed_spawn can be in any dim.
     */
    private static int homeCommand(CommandContext<ServerCommandSource> ctx) {
        ServerPlayerEntity p = onlinePlayer(ctx);
        if (p == null) return 0;

        BlockPos spawn = p.getSpawnPointPosition();
        if (spawn == null) {
            JsonObject err = new JsonObject();
            err.addProperty("ok", false);
            err.addProperty("error", "no spawn point set; sleep in a bed first");
            return reply(ctx, err);
        }
        RegistryKey<World> dim = p.getSpawnPointDimension();
        ServerWorld target = ctx.getSource().getServer().getWorld(dim);
        if (target == null) {
            JsonObject err = new JsonObject();
            err.addProperty("ok", false);
            err.addProperty("error", "spawn dimension unavailable: " + dim.getValue());
            return reply(ctx, err);
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
        return reply(ctx, ok);
    }

    // ---------- nbt_keys (index for unknown mod data) ------------------------
    /**
     * Lists keys at a given NBT path on a player. Top-level only by default;
     * pass a path arg to drill into a specific compound. The pathArg uses
     * vanilla NBT path syntax (same as `data get entity` accepts), so for
     * keys with colons / dots use quoted segments:
     *   nbt_keys X                                — top-level
     *   nbt_keys X cardinal_components             — list CCA component keys
     *   nbt_keys X "cardinal_components.\"trinkets:trinkets\""  — drill in
     *
     * Output is intentionally compact — only key names + types, no values.
     * For the actual values use `data get entity <player> <path>` directly.
     */
    private static int queryNbtKeys(CommandContext<ServerCommandSource> ctx, String pathArg) {
        ServerPlayerEntity p = onlinePlayer(ctx);
        if (p == null) return 0;
        net.minecraft.nbt.NbtCompound full = new net.minecraft.nbt.NbtCompound();
        p.writeNbt(full);

        net.minecraft.nbt.NbtCompound target = full;
        if (pathArg != null && !pathArg.isBlank()) {
            try {
                var path = net.minecraft.command.argument.NbtPathArgumentType.nbtPath()
                    .parse(new com.mojang.brigadier.StringReader(pathArg));
                var elements = path.get(full);
                if (elements.isEmpty()) {
                    return error(ctx, "no element at path: " + pathArg);
                }
                net.minecraft.nbt.NbtElement el = elements.get(0);
                if (el instanceof net.minecraft.nbt.NbtCompound c) {
                    target = c;
                } else {
                    JsonObject leaf = new JsonObject();
                    leaf.addProperty("player", p.getName().getString());
                    leaf.addProperty("path", pathArg);
                    leaf.addProperty("type", nbtTypeName(el));
                    leaf.addProperty("hint", "this path is not a compound; use `data get entity` to read the value");
                    return reply(ctx, leaf);
                }
            } catch (Exception e) {
                return error(ctx, "bad path '" + pathArg + "': " + e.getMessage());
            }
        }

        JsonObject root = new JsonObject();
        root.addProperty("player", p.getName().getString());
        if (pathArg != null) root.addProperty("path", pathArg);
        JsonObject keys = new JsonObject();
        for (String k : target.getKeys()) {
            keys.addProperty(k, nbtTypeName(target.get(k)));
        }
        root.add("keys", keys);
        root.addProperty("count", target.getKeys().size());
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
    // Package-private so ClaudeWriteCommand (and any future sibling commands)
    // can reuse the same canonical reply/error/onlinePlayer paths.
    static ServerPlayerEntity onlinePlayer(CommandContext<ServerCommandSource> ctx) {
        String name = StringArgumentType.getString(ctx, "player");
        ServerPlayerEntity p = ctx.getSource().getServer().getPlayerManager().getPlayer(name);
        if (p == null) {
            error(ctx, "player not online: " + name);
            return null;
        }
        return p;
    }

    static int reply(CommandContext<ServerCommandSource> ctx, JsonObject o) {
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

    static int error(CommandContext<ServerCommandSource> ctx, String msg) {
        JsonObject o = new JsonObject();
        o.addProperty("error", msg);
        ctx.getSource().sendFeedback(() -> Text.literal(GSON.toJson(o)), false);
        return 0;
    }
}
