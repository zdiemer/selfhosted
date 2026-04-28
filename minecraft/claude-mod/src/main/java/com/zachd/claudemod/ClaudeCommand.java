package com.zachd.claudemod;

import com.mojang.brigadier.CommandDispatcher;
import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.exceptions.CommandSyntaxException;

import me.lucko.fabric.api.permissions.v0.Permissions;

import net.minecraft.server.command.CommandManager;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.text.Text;
import net.minecraft.util.Formatting;

public final class ClaudeCommand {
    private ClaudeCommand() {}

    public static void register(CommandDispatcher<ServerCommandSource> dispatcher) {
        dispatcher.register(
            CommandManager.literal("claude")
                .requires(src -> Permissions.check(src, "claude.use", 0))
                .then(CommandManager.argument("prompt", StringArgumentType.greedyString())
                    .executes(ClaudeCommand::run))
        );
    }

    private static int run(com.mojang.brigadier.context.CommandContext<ServerCommandSource> ctx) {
        ServerPlayerEntity player;
        try {
            player = ctx.getSource().getPlayerOrThrow();
        } catch (CommandSyntaxException e) {
            ctx.getSource().sendError(Text.literal("/claude must be run by a player"));
            return 0;
        }
        String prompt = StringArgumentType.getString(ctx, "prompt").trim();
        if (prompt.isEmpty()) {
            ctx.getSource().sendError(Text.literal("usage: /claude <prompt>"));
            return 0;
        }

        // Stdout — the claude-bridge sidecar tails the pod log and matches
        // this exact prefix via COMMAND_RE in bridge.py. Keep the format
        // stable: "[ClaudeRequest] <player>: <prompt>".
        ClaudeMod.LOG.info("[ClaudeRequest] {}: {}", player.getName().getString(), prompt);

        // Instant feedback to the asking player only — bridge will follow
        // up with its own broadcast within a couple seconds.
        ctx.getSource().sendFeedback(
            () -> Text.literal("thinking…").formatted(Formatting.GRAY, Formatting.ITALIC),
            false
        );
        return 1;
    }
}
