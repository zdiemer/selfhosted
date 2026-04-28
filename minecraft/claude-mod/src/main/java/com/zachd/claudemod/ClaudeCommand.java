package com.zachd.claudemod;

import com.mojang.brigadier.CommandDispatcher;
import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.exceptions.CommandSyntaxException;

import me.lucko.fabric.api.permissions.v0.Permissions;

import net.minecraft.server.command.CommandManager;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.text.MutableText;
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

        // Echo the prompt to every online player so the conversation is
        // visible — otherwise everyone sees Claude's reply in chat with
        // no context for what was asked. Slash commands themselves are
        // private to the executor; this broadcast restores conversational
        // flow without going through the public chat event (so
        // dcintegration's relay still ignores it).
        String name = player.getName().getString();
        MutableText echo = Text.literal("[" + name + " → Claude] ")
            .formatted(Formatting.AQUA, Formatting.BOLD)
            .append(Text.literal(prompt).formatted(Formatting.WHITE)
                .styled(s -> s.withBold(false)));
        ctx.getSource().getServer().getPlayerManager().broadcast(echo, false);
        return 1;
    }
}
