package com.zachd.claudemod.write;

import java.util.Locale;

import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.context.CommandContext;

import net.minecraft.server.command.ServerCommandSource;

/**
 * Commit modes:
 *   DEFAULT  — full safety: hopper check, viewer guard, per-target item-conservation
 *   RESTORE  — for `undo`: skip hopper check (so a hopper added after the original
 *              commit can't permanently brick undo) and skip conservation (the
 *              snapshot is authoritative for the layout we're restoring)
 *   MULTI    — for cross-container reorgs orchestrated by the bridge: keep hopper
 *              and viewer checks but skip per-target conservation (the bridge
 *              enforces conservation across the full set of targets, not within one)
 */
public enum Mode {
    DEFAULT, RESTORE, MULTI;

    public static Mode parse(CommandContext<ServerCommandSource> ctx) {
        try {
            String m = StringArgumentType.getString(ctx, "mode").toLowerCase(Locale.ROOT);
            return switch (m) {
                case "restore" -> RESTORE;
                case "multi" -> MULTI;
                default -> DEFAULT;
            };
        } catch (IllegalArgumentException e) {
            return DEFAULT;
        }
    }
}
