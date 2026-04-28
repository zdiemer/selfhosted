package com.zachd.claudemod.query;

import java.util.LinkedHashMap;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import com.mojang.brigadier.context.CommandContext;

import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.enchantment.EnchantmentHelper;
import net.minecraft.entity.EquipmentSlot;
import net.minecraft.entity.attribute.EntityAttribute;
import net.minecraft.entity.attribute.EntityAttributeInstance;
import net.minecraft.entity.attribute.EntityAttributeModifier;
import net.minecraft.entity.player.PlayerInventory;
import net.minecraft.item.ItemStack;
import net.minecraft.nbt.NbtCompound;
import net.minecraft.registry.Registries;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.util.Identifier;

import com.zachd.claudemod.ClaudeMod;
import com.zachd.claudemod.shared.ClaudeIo;

/**
 * Per-player item / equipment / spellbook queries.
 *
 * {@link #stackJson} is the canonical single-stack serializer reused by
 * other query classes (e.g. recipe outputs in {@link RegistryQueries}).
 */
public final class InventoryQueries {
    private InventoryQueries() {}

    /**
     * { "player":"X", "main_hand":{...}, "off_hand":{...},
     *   "armor":{"head":{...}, "chest":{...}, "legs":{...}, "feet":{...}},
     *   "hotbar":[{...} x9], "main":[{...} x27], "ender_chest":[{...}] }
     */
    public static int queryInventory(CommandContext<ServerCommandSource> ctx) {
        ServerPlayerEntity p = ClaudeIo.onlinePlayer(ctx);
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

        return ClaudeIo.reply(ctx, root);
    }

    /**
     * Each stack: {@code { "id":"minecraft:stone", "count":42,
     * "name":"<custom display name or null>",
     * "enchants":["sharpness V","unbreaking III"] }}.
     * Adds {@code tier} / {@code durability_mod} for items decorated by the
     * Tiered mod.
     */
    public static JsonElement stackJson(ItemStack stack) {
        if (stack == null || stack.isEmpty()) return JsonNull.INSTANCE;
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
            NbtCompound nbt = stack.getNbt();
            if (nbt.contains("Tiered", 10)) {
                NbtCompound tiered = nbt.getCompound("Tiered");
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

    /**
     * { "player":"X", "level":27, "progress":0.43, "next_level_xp":67,
     *   "total_xp_collected":1234 }
     */
    public static int queryXp(CommandContext<ServerCommandSource> ctx) {
        ServerPlayerEntity p = ClaudeIo.onlinePlayer(ctx);
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
        return ClaudeIo.reply(ctx, o);
    }

    /**
     * Trinkets API: equipped items in extra slots beyond armor (chest, head,
     * face, ring, belt, gloves, etc., depending on the modpack). Only works
     * for online players.
     */
    public static int queryTrinkets(CommandContext<ServerCommandSource> ctx) {
        if (!FabricLoader.getInstance().isModLoaded("trinkets")) {
            return ClaudeIo.error(ctx, "trinkets mod not loaded");
        }
        ServerPlayerEntity p = ClaudeIo.onlinePlayer(ctx);
        if (p == null) return 0;

        JsonObject root = new JsonObject();
        root.addProperty("player", p.getName().getString());

        try {
            var tcOpt = dev.emi.trinkets.api.TrinketsApi.getTrinketComponent(p);
            if (tcOpt.isEmpty()) {
                root.addProperty("note", "no trinket component for this player");
                root.addProperty("total_equipped", 0);
                return ClaudeIo.reply(ctx, root);
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
            return ClaudeIo.error(ctx, "trinkets api call failed: " + t.toString());
        }
        return ClaudeIo.reply(ctx, root);
    }

    /**
     * Per-equipment-slot breakdown of which item contributes which
     * attribute modifier — answers "is this new sword better?", "where's
     * my crit chance coming from?".
     *
     * For each of main_hand, off_hand, head/chest/legs/feet, walks the
     * stack's {@code getAttributeModifiers(slot)}. Apotheosis/Zenith plug
     * affix modifiers into the same map, so we get them for free.
     */
    public static int queryGear(CommandContext<ServerCommandSource> ctx) {
        ServerPlayerEntity p = ClaudeIo.onlinePlayer(ctx);
        if (p == null) return 0;

        JsonObject root = new JsonObject();
        root.addProperty("player", p.getName().getString());

        // Tracking: per-attribute sum across all slots, addition-only (the
        // common case; multiplicative modifiers are reported per slot but
        // not summed here since they don't combine linearly).
        LinkedHashMap<Identifier, Double> additionSums = new LinkedHashMap<>();

        JsonObject slots = new JsonObject();
        var slotMap = new LinkedHashMap<String, EquipmentSlot>();
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
                    m.addProperty("op", shortOp(mod.getOperation()));
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
        return ClaudeIo.reply(ctx, root);
    }

    private static String shortOp(EntityAttributeModifier.Operation op) {
        return switch (op) {
            case ADDITION -> "+";
            case MULTIPLY_BASE -> "×b";
            case MULTIPLY_TOTAL -> "×t";
        };
    }

    /**
     * Surface the equipped Travelers Backpack's storage / tool slots /
     * upgrades. Empty slots are omitted to keep the response small.
     * Skipped if the mod isn't loaded or the player isn't wearing one.
     */
    public static int queryBackpack(CommandContext<ServerCommandSource> ctx) {
        if (!FabricLoader.getInstance().isModLoaded("travelersbackpack")) {
            return ClaudeIo.error(ctx, "travelersbackpack not loaded");
        }
        ServerPlayerEntity p = ClaudeIo.onlinePlayer(ctx);
        if (p == null) return 0;

        JsonObject root = new JsonObject();
        root.addProperty("player", p.getName().getString());

        try {
            if (!com.tiviacz.travelersbackpack.component.ComponentUtils.isWearingBackpack(p)) {
                root.addProperty("equipped", false);
                return ClaudeIo.reply(ctx, root);
            }
            ItemStack bpStack = com.tiviacz.travelersbackpack.component.ComponentUtils.getWearingBackpack(p);
            var wrapper = com.tiviacz.travelersbackpack.component.ComponentUtils.getBackpackWrapper(p);
            root.addProperty("equipped", true);
            Identifier bid = Registries.ITEM.getId(bpStack.getItem());
            root.addProperty("backpack_id", bid == null ? "?" : bid.toString());
            root.addProperty("backpack_name", bpStack.getName().getString());

            // storage / tools / upgrades — each is an ItemStackHandler with
            // getSlots() + getStackInSlot(int).
            root.add("storage", dumpItemHandler(wrapper.getStorage()));
            root.add("tools",   dumpItemHandler(wrapper.getTools()));
            root.add("upgrades",dumpItemHandler(wrapper.getUpgrades()));
            root.addProperty("tank_capacity_mb", wrapper.getBackpackTankCapacity());
        } catch (Throwable t) {
            return ClaudeIo.error(ctx, "travelersbackpack api call failed: " + t.toString());
        }
        return ClaudeIo.reply(ctx, root);
    }

    /** Dump non-empty slots of an ItemStackHandler-like inventory to JSON. */
    private static JsonArray dumpItemHandler(Object handler) {
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

    /**
     * Best-effort surface of a player's spell state. Spell Engine 0.x has
     * no per-player "known spells" pool — spells come from items the
     * player has equipped (typically a SpellBookItem in a Trinkets slot).
     * No "mana" — Spell Engine uses cooldowns, not a mana pool.
     */
    public static int querySpells(CommandContext<ServerCommandSource> ctx) {
        ServerPlayerEntity p = ClaudeIo.onlinePlayer(ctx);
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
            if (looksSpellRelated(stack, id)) {
                equipped.add(spellItemJson(stack, id, slot.getName()));
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
                                if (looksSpellRelated(stack, id)) {
                                    equipped.add(spellItemJson(stack, id,
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
        return ClaudeIo.reply(ctx, root);
    }

    private static boolean looksSpellRelated(ItemStack stack, Identifier id) {
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

    private static JsonObject spellItemJson(ItemStack stack, Identifier id, String slotLabel) {
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
}
