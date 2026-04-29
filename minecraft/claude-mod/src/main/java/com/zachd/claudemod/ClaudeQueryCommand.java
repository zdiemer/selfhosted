package com.zachd.claudemod;

import com.mojang.brigadier.CommandDispatcher;
import com.mojang.brigadier.arguments.IntegerArgumentType;
import com.mojang.brigadier.arguments.StringArgumentType;

import net.minecraft.command.argument.IdentifierArgumentType;
import net.minecraft.server.command.CommandManager;
import net.minecraft.server.command.ServerCommandSource;

import com.zachd.claudemod.query.InventoryQueries;
import com.zachd.claudemod.query.PlayerStateQueries;
import com.zachd.claudemod.query.QuestQueries;
import com.zachd.claudemod.query.RegistryQueries;
import com.zachd.claudemod.query.ServerAdminQueries;
import com.zachd.claudemod.query.WorldSearchQueries;

/**
 * Builds the {@code /claudemod} Brigadier tree for everything except the
 * write protocol (that lives in {@link ClaudeWriteCommand}) and BlueMap
 * markers ({@link ClaudeMarkerCommand}). Every leaf {@code .executes(...)}
 * dispatches into one of the topic classes in
 * {@link com.zachd.claudemod.query}.
 *
 * Subcommands are RCON-only ({@code !src.isExecutedByPlayer()}); the bridge
 * exposes them via its allowlist {@code ^claudemod\s+query\b}.
 */
public final class ClaudeQueryCommand {
    private ClaudeQueryCommand() {}

    public static void register(CommandDispatcher<ServerCommandSource> dispatcher) {
        dispatcher.register(
            CommandManager.literal("claudemod")
                .requires(src -> !src.isExecutedByPlayer())
                .then(CommandManager.literal("query")
                    .then(CommandManager.literal("inventory")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(InventoryQueries::queryInventory)))
                    .then(CommandManager.literal("xp")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(InventoryQueries::queryXp)))
                    .then(CommandManager.literal("stats")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ctx -> PlayerStateQueries.queryStats(ctx, null))
                            .then(CommandManager.argument("type", StringArgumentType.word())
                                .executes(ctx -> PlayerStateQueries.queryStats(ctx,
                                    StringArgumentType.getString(ctx, "type"))))))
                    .then(CommandManager.literal("recipes")
                        .then(CommandManager.literal("makes")
                            .then(CommandManager.argument("item", StringArgumentType.greedyString())
                                .executes(ctx -> RegistryQueries.queryRecipes(ctx, true))))
                        .then(CommandManager.literal("uses")
                            .then(CommandManager.argument("item", StringArgumentType.greedyString())
                                .executes(ctx -> RegistryQueries.queryRecipes(ctx, false)))))
                    .then(CommandManager.literal("trinkets")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(InventoryQueries::queryTrinkets)))
                    .then(CommandManager.literal("quest")
                        .then(CommandManager.literal("available")
                            .then(CommandManager.argument("player", StringArgumentType.word())
                                .executes(ctx -> QuestQueries.queryAvailableQuests(ctx, null))
                                .then(CommandManager.argument("chapter", StringArgumentType.greedyString())
                                    .executes(ctx -> QuestQueries.queryAvailableQuests(ctx,
                                        StringArgumentType.getString(ctx, "chapter"))))))
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ctx -> QuestQueries.queryQuest(ctx, null))
                            .then(CommandManager.argument("search", StringArgumentType.greedyString())
                                .executes(ctx -> QuestQueries.queryQuest(ctx,
                                    StringArgumentType.getString(ctx, "search"))))))
                    .then(CommandManager.literal("find")
                        .then(CommandManager.argument("dim", IdentifierArgumentType.identifier())
                            .then(CommandManager.argument("item", StringArgumentType.greedyString())
                                .executes(WorldSearchQueries::queryFind))))
                    .then(CommandManager.literal("blocks")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .then(CommandManager.argument("block", StringArgumentType.greedyString())
                                .executes(ctx -> WorldSearchQueries.queryBlocks(ctx, 4)))))
                    .then(CommandManager.literal("containers")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ctx -> WorldSearchQueries.queryContainers(ctx, 4))
                            .then(CommandManager.argument("radius", IntegerArgumentType.integer(1, 8))
                                .executes(ctx -> WorldSearchQueries.queryContainers(ctx,
                                    IntegerArgumentType.getInteger(ctx, "radius"))))))
                    .then(CommandManager.literal("last_death")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(PlayerStateQueries::queryLastDeath)))
                    .then(CommandManager.literal("level")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(PlayerStateQueries::queryLevel)))
                    .then(CommandManager.literal("items")
                        .then(CommandManager.argument("search", StringArgumentType.greedyString())
                            .executes(RegistryQueries::queryItems)))
                    .then(CommandManager.literal("nbt_keys")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ctx -> PlayerStateQueries.queryNbtKeys(ctx, null))
                            .then(CommandManager.argument("path", StringArgumentType.greedyString())
                                .executes(ctx -> PlayerStateQueries.queryNbtKeys(ctx,
                                    StringArgumentType.getString(ctx, "path"))))))
                    .then(CommandManager.literal("skills")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(PlayerStateQueries::querySkills)))
                    .then(CommandManager.literal("skill_options")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ctx -> PlayerStateQueries.querySkillOptions(ctx, null, null))
                            .then(CommandManager.argument("state", StringArgumentType.word())
                                .executes(ctx -> PlayerStateQueries.querySkillOptions(ctx,
                                    StringArgumentType.getString(ctx, "state"), null))
                                .then(CommandManager.argument("category", StringArgumentType.greedyString())
                                    .executes(ctx -> PlayerStateQueries.querySkillOptions(ctx,
                                        StringArgumentType.getString(ctx, "state"),
                                        StringArgumentType.getString(ctx, "category")))))))
                    .then(CommandManager.literal("mobs")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ctx -> WorldSearchQueries.queryMobs(ctx, 32))
                            .then(CommandManager.argument("radius", IntegerArgumentType.integer(1, 256))
                                .executes(ctx -> WorldSearchQueries.queryMobs(ctx,
                                    IntegerArgumentType.getInteger(ctx, "radius"))))))
                    .then(CommandManager.literal("craftable")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ctx -> RegistryQueries.queryCraftable(ctx, null))
                            .then(CommandManager.argument("filter", StringArgumentType.greedyString())
                                .executes(ctx -> RegistryQueries.queryCraftable(ctx,
                                    StringArgumentType.getString(ctx, "filter"))))))
                    .then(CommandManager.literal("vitals")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(PlayerStateQueries::queryVitals)))
                    .then(CommandManager.literal("item")
                        .then(CommandManager.argument("id", StringArgumentType.greedyString())
                            .executes(RegistryQueries::queryItem)))
                    .then(CommandManager.literal("tag")
                        .then(CommandManager.argument("tag", StringArgumentType.greedyString())
                            .executes(RegistryQueries::queryTag)))
                    .then(CommandManager.literal("mob")
                        .then(CommandManager.argument("id", StringArgumentType.greedyString())
                            .executes(RegistryQueries::queryMob)))
                    .then(CommandManager.literal("mods")
                        .executes(ctx -> RegistryQueries.queryMods(ctx, null))
                        .then(CommandManager.argument("search", StringArgumentType.greedyString())
                            .executes(ctx -> RegistryQueries.queryMods(ctx,
                                StringArgumentType.getString(ctx, "search")))))
                    .then(CommandManager.literal("gear")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(InventoryQueries::queryGear)))
                    .then(CommandManager.literal("backpack")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(InventoryQueries::queryBackpack)))
                    .then(CommandManager.literal("spells")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(InventoryQueries::querySpells)))
                    .then(CommandManager.literal("here")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(PlayerStateQueries::queryHere)))
                    .then(CommandManager.literal("nearest")
                        .then(CommandManager.literal("biome")
                            .then(CommandManager.argument("player", StringArgumentType.word())
                                .then(CommandManager.argument("id", StringArgumentType.greedyString())
                                    .executes(ctx -> WorldSearchQueries.queryNearest(ctx, true)))))
                        .then(CommandManager.literal("structure")
                            .then(CommandManager.argument("player", StringArgumentType.word())
                                .then(CommandManager.argument("id", StringArgumentType.greedyString())
                                    .executes(ctx -> WorldSearchQueries.queryNearest(ctx, false))))))
                    .then(CommandManager.literal("perf")
                        .executes(ServerAdminQueries::queryPerf)))
                .then(CommandManager.literal("home")
                    .then(CommandManager.argument("player", StringArgumentType.word())
                        .executes(WorldSearchQueries::homeCommand)))
                .then(CommandManager.literal("bossbar")
                    .then(CommandManager.literal("update")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .then(CommandManager.argument("text", StringArgumentType.greedyString())
                                .executes(ServerAdminQueries::bossbarUpdate))))
                    .then(CommandManager.literal("remove")
                        .then(CommandManager.argument("player", StringArgumentType.word())
                            .executes(ServerAdminQueries::bossbarRemove))))
        );
    }
}
