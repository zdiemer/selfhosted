package com.zachd.claudemod.shared;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonObject;
import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.context.CommandContext;

import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.text.Text;

/**
 * Canonical RCON I/O helpers shared by every /claudemod handler.
 *
 * Both the read-only query commands and the chunked write protocol go through
 * {@link #reply}/{@link #error} so the bridge sees a single deterministic
 * serialization (HTML-escaping disabled, 3900-char RCON cap).
 */
public final class ClaudeIo {
    private ClaudeIo() {}

    public static final Gson GSON = new GsonBuilder().disableHtmlEscaping().create();
    // RCON output cap. Minecraft's RCON spec allows packets up to 4096
    // bytes; we leave a small margin for protocol framing.
    public static final int MAX_RESPONSE_CHARS = 3900;

    public static ServerPlayerEntity onlinePlayer(CommandContext<ServerCommandSource> ctx) {
        String name = StringArgumentType.getString(ctx, "player");
        ServerPlayerEntity p = ctx.getSource().getServer().getPlayerManager().getPlayer(name);
        if (p == null) {
            error(ctx, "player not online: " + name);
            return null;
        }
        return p;
    }

    public static int reply(CommandContext<ServerCommandSource> ctx, JsonObject o) {
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

    public static int error(CommandContext<ServerCommandSource> ctx, String msg) {
        JsonObject o = new JsonObject();
        o.addProperty("error", msg);
        ctx.getSource().sendFeedback(() -> Text.literal(GSON.toJson(o)), false);
        return 0;
    }

    public static String truncate(String s, int max) {
        if (s == null) return "";
        s = s.replaceAll("\\s+", " ").trim();
        return s.length() <= max ? s : s.substring(0, max - 1) + "…";
    }
}
