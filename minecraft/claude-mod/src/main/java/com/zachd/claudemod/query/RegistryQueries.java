package com.zachd.claudemod.query;

import java.util.ArrayList;
import java.util.Comparator;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.context.CommandContext;

import net.fabricmc.loader.api.FabricLoader;
import net.fabricmc.loader.api.ModContainer;
import net.minecraft.entity.EntityType;
import net.minecraft.entity.LivingEntity;
import net.minecraft.entity.attribute.DefaultAttributeContainer;
import net.minecraft.entity.attribute.DefaultAttributeRegistry;
import net.minecraft.entity.attribute.EntityAttribute;
import net.minecraft.entity.attribute.EntityAttributes;
import net.minecraft.item.Item;
import net.minecraft.item.ItemStack;
import net.minecraft.recipe.AbstractCookingRecipe;
import net.minecraft.recipe.Ingredient;
import net.minecraft.recipe.Recipe;
import net.minecraft.recipe.ShapedRecipe;
import net.minecraft.registry.DynamicRegistryManager;
import net.minecraft.registry.Registries;
import net.minecraft.registry.RegistryKeys;
import net.minecraft.registry.tag.TagKey;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.command.ServerCommandSource;

import com.zachd.claudemod.ClaudeMod;
import com.zachd.claudemod.shared.ClaudeIo;

/**
 * Registry-driven lookups: recipes, items, item tags, mob types, loaded mods.
 *
 * Tag membership drives cross-mod equivalence in modded packs (e.g.
 * {@code #c:ingots/iron} unifies modded iron variants), so {@code item} and
 * {@code tag} are the natural pair Claude reaches for when summarizing
 * unfamiliar inventories.
 */
public final class RegistryQueries {
    private RegistryQueries() {}

    private static final int RECIPE_RESULT_CAP = 20;
    private static final int TAG_RESULT_CAP = 60;
    private static final int MODS_RESULT_CAP = 60;

    public static int queryRecipes(CommandContext<ServerCommandSource> ctx, boolean makes) {
        String raw = StringArgumentType.getString(ctx, "item").trim();
        net.minecraft.util.Identifier id = net.minecraft.util.Identifier.tryParse(
            raw.contains(":") ? raw : ("minecraft:" + raw));
        if (id == null) return ClaudeIo.error(ctx, "bad item id: " + raw);
        Item item = Registries.ITEM.containsId(id) ? Registries.ITEM.get(id) : null;
        if (item == null) return ClaudeIo.error(ctx, "unknown item: " + id);

        MinecraftServer server = ctx.getSource().getServer();
        DynamicRegistryManager registryManager = server.getRegistryManager();

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
        return ClaudeIo.reply(ctx, out);
    }

    private static JsonObject recipeJson(Recipe<?> r, DynamicRegistryManager registryManager) {
        JsonObject o = new JsonObject();
        o.addProperty("id", r.getId().toString());
        net.minecraft.util.Identifier typeId = Registries.RECIPE_TYPE.getId(r.getType());
        o.addProperty("type", typeId == null ? "?" : typeId.toString());

        ItemStack output = r.getOutput(registryManager);
        if (output != null && !output.isEmpty()) {
            o.add("output", InventoryQueries.stackJson(output));
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
        if (ing == null || ing.isEmpty()) return JsonNull.INSTANCE;
        JsonArray arr = new JsonArray();
        for (ItemStack s : ing.getMatchingStacks()) {
            net.minecraft.util.Identifier sid = Registries.ITEM.getId(s.getItem());
            arr.add(sid == null ? "?" : sid.toString());
        }
        return arr;
    }

    /**
     * Translate an item id into a friendlier description: display name,
     * which tags it belongs to (tags drive cross-mod equivalence — e.g.
     * {@code #c:ingots/iron} is what unifies modded iron variants), max
     * stack size, rarity, and the source mod.
     */
    public static int queryItem(CommandContext<ServerCommandSource> ctx) {
        String raw = StringArgumentType.getString(ctx, "id").trim();
        net.minecraft.util.Identifier id = net.minecraft.util.Identifier.tryParse(
            raw.contains(":") ? raw : ("minecraft:" + raw));
        if (id == null) return ClaudeIo.error(ctx, "bad item id: " + raw);
        if (!Registries.ITEM.containsId(id)) return ClaudeIo.error(ctx, "unknown item: " + id);

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
        return ClaudeIo.reply(ctx, out);
    }

    /**
     * List items in an item tag (cross-mod equivalence groups). Player can
     * include or omit the leading '#'; we accept either. Caps at
     * TAG_RESULT_CAP entries — modded packs have very large tags.
     */
    public static int queryTag(CommandContext<ServerCommandSource> ctx) {
        String raw = StringArgumentType.getString(ctx, "tag").trim();
        if (raw.startsWith("#")) raw = raw.substring(1);
        net.minecraft.util.Identifier tagId = net.minecraft.util.Identifier.tryParse(
            raw.contains(":") ? raw : ("minecraft:" + raw));
        if (tagId == null) return ClaudeIo.error(ctx, "bad tag: " + raw);

        TagKey<Item> key = TagKey.of(RegistryKeys.ITEM, tagId);
        var entryListOpt = Registries.ITEM.getEntryList(key);
        if (entryListOpt.isEmpty()) {
            JsonObject empty = new JsonObject();
            empty.addProperty("tag", "#" + tagId);
            empty.addProperty("hits", 0);
            empty.add("items", new JsonArray());
            empty.addProperty("note", "no such tag, or tag is empty");
            return ClaudeIo.reply(ctx, empty);
        }
        JsonArray items = new JsonArray();
        int total = 0;
        boolean truncated = false;
        for (var entry : entryListOpt.get()) {
            if (items.size() >= TAG_RESULT_CAP) { truncated = true; break; }
            net.minecraft.util.Identifier iid = Registries.ITEM.getId(entry.value());
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
        return ClaudeIo.reply(ctx, out);
    }

    /**
     * Static info about an entity type: display name, source mod, default
     * attributes, loot table id. We can't get exact XP drops without
     * instantiating, so we leave that to Claude — the loot table id is
     * enough to look up more if needed.
     */
    public static int queryMob(CommandContext<ServerCommandSource> ctx) {
        String raw = StringArgumentType.getString(ctx, "id").trim();
        net.minecraft.util.Identifier id = net.minecraft.util.Identifier.tryParse(
            raw.contains(":") ? raw : ("minecraft:" + raw));
        if (id == null) return ClaudeIo.error(ctx, "bad entity id: " + raw);
        if (!Registries.ENTITY_TYPE.containsId(id)) return ClaudeIo.error(ctx, "unknown entity: " + id);

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
                EntityAttribute[] keys = new EntityAttribute[] {
                    EntityAttributes.GENERIC_MAX_HEALTH,
                    EntityAttributes.GENERIC_ATTACK_DAMAGE,
                    EntityAttributes.GENERIC_ARMOR,
                    EntityAttributes.GENERIC_MOVEMENT_SPEED,
                    EntityAttributes.GENERIC_FOLLOW_RANGE,
                    EntityAttributes.GENERIC_KNOCKBACK_RESISTANCE,
                };
                for (EntityAttribute k : keys) {
                    if (attrs.has(k)) {
                        net.minecraft.util.Identifier aid = Registries.ATTRIBUTE.getId(k);
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
        return ClaudeIo.reply(ctx, out);
    }

    /**
     * List loaded Fabric mods. Without a search arg, returns the count plus
     * the first MODS_RESULT_CAP sorted by id. With a search arg, returns
     * mods whose id, name, or description (case-insensitive substring) match.
     */
    public static int queryMods(CommandContext<ServerCommandSource> ctx, String search) {
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
        return ClaudeIo.reply(ctx, root);
    }

    private static JsonObject modJson(ModContainer m) {
        var md = m.getMetadata();
        JsonObject o = new JsonObject();
        o.addProperty("id", md.getId());
        o.addProperty("name", md.getName());
        o.addProperty("version", md.getVersion().getFriendlyString());
        String desc = md.getDescription();
        if (desc != null && !desc.isEmpty()) {
            o.addProperty("description", ClaudeIo.truncate(desc, 200));
        }
        return o;
    }
}
