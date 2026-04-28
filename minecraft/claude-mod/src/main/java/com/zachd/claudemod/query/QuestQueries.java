package com.zachd.claudemod.query;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.mojang.brigadier.context.CommandContext;

import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.network.ServerPlayerEntity;

import com.zachd.claudemod.shared.ClaudeIo;

/**
 * /claudemod query quest [available] — FTB Quests progress + lookup.
 *
 * Per-team progress: FTB Quests scopes progress per team; we look up the
 * player's team data via {@code getNullableTeamData}. Casts to
 * {@code QuestObjectBase} dodge {@code Movable.getTitle()}'s abstract
 * interface dispatch — without the cast javac emits invokeinterface and
 * the JVM throws AbstractMethodError at runtime.
 */
public final class QuestQueries {
    private QuestQueries() {}

    private static final int QUEST_HIT_CAP = 15;

    public static int queryQuest(CommandContext<ServerCommandSource> ctx, String search) {
        if (!FabricLoader.getInstance().isModLoaded("ftbquests")) {
            return ClaudeIo.error(ctx, "ftb-quests not loaded");
        }
        ServerPlayerEntity p = ClaudeIo.onlinePlayer(ctx);
        if (p == null) return 0;

        try {
            var sqf = dev.ftb.mods.ftbquests.quest.ServerQuestFile.INSTANCE;
            if (sqf == null) return ClaudeIo.error(ctx, "FTB Quests not initialized yet");
            var td = sqf.getNullableTeamData(p.getUuid());

            JsonObject root = new JsonObject();
            root.addProperty("player", p.getName().getString());
            if (td == null) {
                root.addProperty("note", "no team data for this player");
                return ClaudeIo.reply(ctx, root);
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
                    String title = ((dev.ftb.mods.ftbquests.quest.QuestObjectBase) q)
                        .getRawTitle();
                    if (title == null) title = "";
                    String desc = describeQuest(q);
                    String hay = (title + " | " + desc).toLowerCase();
                    if (!hay.contains(needle)) return;
                    JsonObject h = new JsonObject();
                    h.addProperty("title", title);
                    h.addProperty("description", ClaudeIo.truncate(desc, 600));
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
            return ClaudeIo.reply(ctx, root);
        } catch (Throwable t) {
            return ClaudeIo.error(ctx, "ftb-quests api call failed: " + t.toString());
        }
    }

    /**
     * Quests the player can START RIGHT NOW: not completed, dependencies
     * satisfied, and visible to their team. Optional chapter filter is a
     * substring match on chapter title.
     */
    public static int queryAvailableQuests(CommandContext<ServerCommandSource> ctx, String chapterFilter) {
        if (!FabricLoader.getInstance().isModLoaded("ftbquests")) {
            return ClaudeIo.error(ctx, "ftb-quests not loaded");
        }
        ServerPlayerEntity p = ClaudeIo.onlinePlayer(ctx);
        if (p == null) return 0;

        try {
            var sqf = dev.ftb.mods.ftbquests.quest.ServerQuestFile.INSTANCE;
            if (sqf == null) return ClaudeIo.error(ctx, "FTB Quests not initialized yet");
            var td = sqf.getNullableTeamData(p.getUuid());
            if (td == null) {
                JsonObject root = new JsonObject();
                root.addProperty("player", p.getName().getString());
                root.addProperty("note", "no team data for this player");
                return ClaudeIo.reply(ctx, root);
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
                h.addProperty("description", ClaudeIo.truncate(describeQuest(q), 400));
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
            return ClaudeIo.reply(ctx, root);
        } catch (Throwable t) {
            return ClaudeIo.error(ctx, "ftb-quests api call failed: " + t.toString());
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
}
