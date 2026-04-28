package com.zachd.claudemod.query;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.UUID;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.mojang.brigadier.StringReader;
import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.context.CommandContext;

import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.command.argument.NbtPathArgumentType;
import net.minecraft.entity.attribute.EntityAttribute;
import net.minecraft.entity.attribute.EntityAttributeInstance;
import net.minecraft.entity.attribute.EntityAttributes;
import net.minecraft.entity.effect.StatusEffectInstance;
import net.minecraft.nbt.NbtCompound;
import net.minecraft.nbt.NbtElement;
import net.minecraft.nbt.NbtList;
import net.minecraft.registry.Registries;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.Identifier;
import net.minecraft.util.WorldSavePath;
import net.minecraft.util.math.BlockPos;
import net.minecraft.world.LightType;

import com.zachd.claudemod.ClaudeMod;
import com.zachd.claudemod.shared.ClaudeIo;

/**
 * Live and persisted per-player state: vanilla stats file, vitals (health
 * + effects + resolved attributes), Puffish/SimplySkills tree state,
 * current-position context, and NBT key introspection.
 */
public final class PlayerStateQueries {
    private PlayerStateQueries() {}

    // Per-category cap on stat entries returned, sorted by count desc.
    // Modded servers have thousands of stat keys; capping keeps the response
    // under the RCON budget.
    private static final int STATS_TOP_N = 30;

    /**
     * Reads the saved stats file at world/stats/uuid.json. Sub-objects:
     *   minecraft:mined, minecraft:crafted, minecraft:used, minecraft:broken,
     *   minecraft:picked_up, minecraft:dropped, minecraft:killed,
     *   minecraft:killed_by, minecraft:custom
     *
     * Limitation: vanilla saves stats every ~5 minutes for online players,
     * so very recent activity may not be reflected. We force a flush via
     * ServerStatHandler.save() before reading.
     */
    public static int queryStats(CommandContext<ServerCommandSource> ctx, String typeFilter) {
        String name = StringArgumentType.getString(ctx, "player");
        MinecraftServer server = ctx.getSource().getServer();

        // Flush online player's stats so the file we're about to read is fresh.
        ServerPlayerEntity online = server.getPlayerManager().getPlayer(name);
        if (online != null) online.getStatHandler().save();

        // Resolve UUID — prefer online player; fall back to UserCache for offline.
        UUID uuid = online != null ? online.getUuid()
            : server.getUserCache() == null
                ? null
                : server.getUserCache().findByName(name).map(p -> p.getId()).orElse(null);
        if (uuid == null) {
            return ClaudeIo.error(ctx, "unknown player: " + name);
        }

        Path statsFile = server.getSavePath(WorldSavePath.STATS).resolve(uuid + ".json");
        if (!Files.exists(statsFile)) {
            return ClaudeIo.reply(ctx, emptyStats(name));
        }

        JsonObject parsed;
        try {
            parsed = ClaudeIo.GSON.fromJson(Files.readString(statsFile), JsonObject.class);
        } catch (IOException e) {
            return ClaudeIo.error(ctx, "stats file unreadable: " + e.getMessage());
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
        return ClaudeIo.reply(ctx, out);
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

    /**
     * Live player vitals: health, hunger, air, status effects (with duration
     * + amplifier), and the resolved values of common attributes including
     * any modifiers from gear, affixes, skills, etc.
     */
    public static int queryVitals(CommandContext<ServerCommandSource> ctx) {
        ServerPlayerEntity p = ClaudeIo.onlinePlayer(ctx);
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
        EntityAttribute[] attributes = new EntityAttribute[] {
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
        for (EntityAttribute attr : attributes) {
            EntityAttributeInstance inst = p.getAttributeInstance(attr);
            if (inst == null) continue;
            Identifier aid = Registries.ATTRIBUTE.getId(attr);
            JsonObject a = new JsonObject();
            a.addProperty("base", inst.getBaseValue());
            a.addProperty("value", inst.getValue());
            attrs.add(aid == null ? "?" : aid.toString(), a);
        }
        root.add("attributes", attrs);
        return ClaudeIo.reply(ctx, root);
    }

    /**
     * Per-player skill-tree state from Puffish Skills. SimplySkills is built
     * ON Puffish — its specializations ARE puffish categories, so this
     * single query covers both.
     */
    public static int querySkills(CommandContext<ServerCommandSource> ctx) {
        if (!FabricLoader.getInstance().isModLoaded("puffish_skills")) {
            return ClaudeIo.error(ctx, "puffish_skills not loaded");
        }
        ServerPlayerEntity p = ClaudeIo.onlinePlayer(ctx);
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
            return ClaudeIo.error(ctx, "puffish_skills api call failed: " + t.toString());
        }

        root.add("categories", categories);
        return ClaudeIo.reply(ctx, root);
    }

    /**
     * Ground-truth context about a player's CURRENT position: coords +
     * dimension + biome at that pos + light levels + the block at their
     * feet + whether the sky is visible + whether it's raining here.
     * Vanilla has no "what biome am I in?" command — {@code /locate biome}
     * only finds the nearest match, not the current one.
     */
    public static int queryHere(CommandContext<ServerCommandSource> ctx) {
        ServerPlayerEntity p = ClaudeIo.onlinePlayer(ctx);
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
        light.addProperty("block", world.getLightLevel(LightType.BLOCK, pos));
        light.addProperty("sky", world.getLightLevel(LightType.SKY, pos));
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

        root.addProperty("time_of_day", (int) (world.getTimeOfDay() % 24000));
        return ClaudeIo.reply(ctx, root);
    }

    /**
     * Lists keys at a given NBT path on a player. Top-level only by default;
     * pass a path arg to drill into a specific compound. The pathArg uses
     * vanilla NBT path syntax — for keys with colons / dots use quoted
     * segments, e.g.
     * {@code nbt_keys X "cardinal_components.\"trinkets:trinkets\""}.
     *
     * Output is intentionally compact — only key names + types, no values.
     * For the actual values use {@code data get entity <player> <path>}.
     */
    public static int queryNbtKeys(CommandContext<ServerCommandSource> ctx, String pathArg) {
        ServerPlayerEntity p = ClaudeIo.onlinePlayer(ctx);
        if (p == null) return 0;
        NbtCompound full = new NbtCompound();
        p.writeNbt(full);

        NbtCompound target = full;
        if (pathArg != null && !pathArg.isBlank()) {
            try {
                var path = NbtPathArgumentType.nbtPath().parse(new StringReader(pathArg));
                var elements = path.get(full);
                if (elements.isEmpty()) {
                    return ClaudeIo.error(ctx, "no element at path: " + pathArg);
                }
                NbtElement el = elements.get(0);
                if (el instanceof NbtCompound c) {
                    target = c;
                } else {
                    JsonObject leaf = new JsonObject();
                    leaf.addProperty("player", p.getName().getString());
                    leaf.addProperty("path", pathArg);
                    leaf.addProperty("type", nbtTypeName(el));
                    leaf.addProperty("hint", "this path is not a compound; use `data get entity` to read the value");
                    return ClaudeIo.reply(ctx, leaf);
                }
            } catch (Exception e) {
                return ClaudeIo.error(ctx, "bad path '" + pathArg + "': " + e.getMessage());
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
        return ClaudeIo.reply(ctx, root);
    }

    private static String nbtTypeName(NbtElement el) {
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
            case 9 -> "list[" + ((NbtList) el).size() + "]";
            case 10 -> "compound{" + ((NbtCompound) el).getSize() + "}";
            case 11 -> "int[]";
            case 12 -> "long[]";
            default -> "?";
        };
    }
}
