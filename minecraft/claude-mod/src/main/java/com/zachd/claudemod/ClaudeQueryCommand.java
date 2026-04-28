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

import net.minecraft.enchantment.EnchantmentHelper;
import net.minecraft.entity.player.PlayerInventory;
import net.minecraft.item.ItemStack;
import net.minecraft.recipe.Ingredient;
import net.minecraft.recipe.Recipe;
import net.minecraft.recipe.ShapedRecipe;
import net.minecraft.recipe.ShapelessRecipe;
import net.minecraft.recipe.AbstractCookingRecipe;
import net.minecraft.registry.Registries;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.command.CommandManager;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.text.Text;
import net.minecraft.util.Identifier;
import net.minecraft.util.WorldSavePath;

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
                                .executes(ctx -> queryRecipes(ctx, false))))))
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
