package com.zachd.claudemod;

import com.mojang.brigadier.CommandDispatcher;
import com.mojang.brigadier.arguments.IntegerArgumentType;
import com.mojang.brigadier.arguments.StringArgumentType;

import net.minecraft.command.argument.IdentifierArgumentType;
import net.minecraft.server.command.CommandManager;
import net.minecraft.server.command.ServerCommandSource;

import com.zachd.claudemod.write.Kind;
import com.zachd.claudemod.write.Mode;
import com.zachd.claudemod.write.PendingTxn;
import com.zachd.claudemod.write.TxnHandlers;

/**
 * RCON-only chunked write protocol. Lets the bridge propose and atomically
 * apply a new layout to a container or player inventory after running
 * server-authoritative safety checks (distance, hoppers, viewers, item
 * conservation, TOCTOU contents hash).
 *
 * Protocol (all under /claudemod write, gated to non-player sources):
 *
 *   READ
 *     read_open container <caller> <dim> <x> <y> <z>
 *     read_open inventory <caller>
 *     read_open backpack_equipped <caller>
 *     read_open backpack_world <caller> <dim> <x> <y> <z>
 *       -> {txn_id, kind, half, total_slots, contents_hash}
 *     read_slot <txn_id> <slot>
 *       -> {} | {"i": "<id>", "c": <count>, "n": "<base64-nbt-or-omitted>"}
 *     read_close <txn_id>
 *
 *   WRITE
 *     txn_open container <caller> <dim> <x> <y> <z> <expected_hash> [<mode>]
 *     txn_open inventory <caller> <expected_hash> [<mode>]
 *     txn_open backpack_equipped <caller> <expected_hash> [<mode>]
 *     txn_open backpack_world <caller> <dim> <x> <y> <z> <expected_hash> [<mode>]
 *       mode: "default" (with conservation check) or "restore" (skip hopper check, rely on hash)
 *       -> {txn_id, total_slots}
 *     txn_slot <txn_id> <slot> <stack_b64_or_dash>
 *       -> {}
 *     txn_commit <txn_id>
 *       -> {ok: true, applied: <n>, snapshot_pre_hash: "<sha256>"}
 *     txn_abort <txn_id>
 *
 * Slot indexing for {@code inventory} is dense 0..26 mapped to PlayerInventory
 * slots 9..35 — hotbar (0..8), armor, off-hand are intentionally
 * unreachable from this protocol.
 *
 * Slot indexing for {@code backpack_*} is dense 0..N-1 over the wearable's
 * storage handler only — tools, upgrades, fluid tanks are unreachable.
 */
public final class ClaudeWriteCommand {
    private ClaudeWriteCommand() {}

    /** Periodic cleanup; called from ClaudeMod's tick handler. */
    public static void sweep() {
        PendingTxn.sweep();
    }

    public static void register(CommandDispatcher<ServerCommandSource> dispatcher) {
        dispatcher.register(
            CommandManager.literal("claudemod")
                .requires(src -> !src.isExecutedByPlayer())
                .then(CommandManager.literal("write")
                    // ----- read_open -----
                    .then(CommandManager.literal("read_open")
                        .then(CommandManager.literal("container")
                            .then(CommandManager.argument("caller", StringArgumentType.word())
                                .then(CommandManager.argument("dim", IdentifierArgumentType.identifier())
                                    .then(CommandManager.argument("x", IntegerArgumentType.integer())
                                        .then(CommandManager.argument("y", IntegerArgumentType.integer())
                                            .then(CommandManager.argument("z", IntegerArgumentType.integer())
                                                .executes(ctx -> TxnHandlers.openTxn(ctx, Kind.CONTAINER, false, Mode.DEFAULT, true))))))))
                        .then(CommandManager.literal("inventory")
                            .then(CommandManager.argument("caller", StringArgumentType.word())
                                .executes(ctx -> TxnHandlers.openTxn(ctx, Kind.INVENTORY, false, Mode.DEFAULT, false))))
                        .then(CommandManager.literal("backpack_equipped")
                            .then(CommandManager.argument("caller", StringArgumentType.word())
                                .executes(ctx -> TxnHandlers.openTxn(ctx, Kind.BACKPACK_EQUIPPED, false, Mode.DEFAULT, false))))
                        .then(CommandManager.literal("backpack_world")
                            .then(CommandManager.argument("caller", StringArgumentType.word())
                                .then(CommandManager.argument("dim", IdentifierArgumentType.identifier())
                                    .then(CommandManager.argument("x", IntegerArgumentType.integer())
                                        .then(CommandManager.argument("y", IntegerArgumentType.integer())
                                            .then(CommandManager.argument("z", IntegerArgumentType.integer())
                                                .executes(ctx -> TxnHandlers.openTxn(ctx, Kind.BACKPACK_WORLD, false, Mode.DEFAULT, true))))))))
                    )
                    // ----- read_slot -----
                    .then(CommandManager.literal("read_slot")
                        .then(CommandManager.argument("txn_id", StringArgumentType.word())
                            .then(CommandManager.argument("slot", IntegerArgumentType.integer(0))
                                .executes(TxnHandlers::readSlot))))
                    // ----- read_slot_part (chunked read for large NBT) -----
                    .then(CommandManager.literal("read_slot_part")
                        .then(CommandManager.argument("txn_id", StringArgumentType.word())
                            .then(CommandManager.argument("slot", IntegerArgumentType.integer(0))
                                .then(CommandManager.argument("chunk_idx", IntegerArgumentType.integer(0))
                                    .executes(TxnHandlers::readSlotPart)))))
                    // ----- read_close -----
                    .then(CommandManager.literal("read_close")
                        .then(CommandManager.argument("txn_id", StringArgumentType.word())
                            .executes(TxnHandlers::closeTxn)))

                    // ----- txn_open (write) -----
                    .then(CommandManager.literal("txn_open")
                        .then(CommandManager.literal("container")
                            .then(CommandManager.argument("caller", StringArgumentType.word())
                                .then(CommandManager.argument("dim", IdentifierArgumentType.identifier())
                                    .then(CommandManager.argument("x", IntegerArgumentType.integer())
                                        .then(CommandManager.argument("y", IntegerArgumentType.integer())
                                            .then(CommandManager.argument("z", IntegerArgumentType.integer())
                                                .then(CommandManager.argument("hash", StringArgumentType.word())
                                                    .executes(ctx -> TxnHandlers.openTxn(ctx, Kind.CONTAINER, true, Mode.DEFAULT, true))
                                                    .then(CommandManager.argument("mode", StringArgumentType.word())
                                                        .executes(ctx -> TxnHandlers.openTxn(ctx, Kind.CONTAINER, true, Mode.parse(ctx), true))))))))))
                        .then(CommandManager.literal("inventory")
                            .then(CommandManager.argument("caller", StringArgumentType.word())
                                .then(CommandManager.argument("hash", StringArgumentType.word())
                                    .executes(ctx -> TxnHandlers.openTxn(ctx, Kind.INVENTORY, true, Mode.DEFAULT, false))
                                    .then(CommandManager.argument("mode", StringArgumentType.word())
                                        .executes(ctx -> TxnHandlers.openTxn(ctx, Kind.INVENTORY, true, Mode.parse(ctx), false))))))
                        .then(CommandManager.literal("backpack_equipped")
                            .then(CommandManager.argument("caller", StringArgumentType.word())
                                .then(CommandManager.argument("hash", StringArgumentType.word())
                                    .executes(ctx -> TxnHandlers.openTxn(ctx, Kind.BACKPACK_EQUIPPED, true, Mode.DEFAULT, false))
                                    .then(CommandManager.argument("mode", StringArgumentType.word())
                                        .executes(ctx -> TxnHandlers.openTxn(ctx, Kind.BACKPACK_EQUIPPED, true, Mode.parse(ctx), false))))))
                        .then(CommandManager.literal("backpack_world")
                            .then(CommandManager.argument("caller", StringArgumentType.word())
                                .then(CommandManager.argument("dim", IdentifierArgumentType.identifier())
                                    .then(CommandManager.argument("x", IntegerArgumentType.integer())
                                        .then(CommandManager.argument("y", IntegerArgumentType.integer())
                                            .then(CommandManager.argument("z", IntegerArgumentType.integer())
                                                .then(CommandManager.argument("hash", StringArgumentType.word())
                                                    .executes(ctx -> TxnHandlers.openTxn(ctx, Kind.BACKPACK_WORLD, true, Mode.DEFAULT, true))
                                                    .then(CommandManager.argument("mode", StringArgumentType.word())
                                                        .executes(ctx -> TxnHandlers.openTxn(ctx, Kind.BACKPACK_WORLD, true, Mode.parse(ctx), true))))))))))
                    )
                    // ----- txn_slot (single-shot, small NBT) -----
                    .then(CommandManager.literal("txn_slot")
                        .then(CommandManager.argument("txn_id", StringArgumentType.word())
                            .then(CommandManager.argument("slot", IntegerArgumentType.integer(0))
                                .then(CommandManager.argument("stack", StringArgumentType.greedyString())
                                    .executes(TxnHandlers::txnSlot)))))
                    // ----- txn_slot_part (chunked write — append a fragment) -----
                    .then(CommandManager.literal("txn_slot_part")
                        .then(CommandManager.argument("txn_id", StringArgumentType.word())
                            .then(CommandManager.argument("slot", IntegerArgumentType.integer(0))
                                .then(CommandManager.argument("chunk_idx", IntegerArgumentType.integer(0))
                                    .then(CommandManager.argument("part", StringArgumentType.greedyString())
                                        .executes(TxnHandlers::txnSlotPart))))))
                    // ----- txn_slot_finish (chunked write — finalize, build stack) -----
                    .then(CommandManager.literal("txn_slot_finish")
                        .then(CommandManager.argument("txn_id", StringArgumentType.word())
                            .then(CommandManager.argument("slot", IntegerArgumentType.integer(0))
                                .then(CommandManager.argument("count", IntegerArgumentType.integer(1))
                                    .executes(TxnHandlers::txnSlotFinish)))))
                    // ----- txn_commit -----
                    .then(CommandManager.literal("txn_commit")
                        .then(CommandManager.argument("txn_id", StringArgumentType.word())
                            .executes(TxnHandlers::txnCommit)))
                    // ----- txn_abort -----
                    .then(CommandManager.literal("txn_abort")
                        .then(CommandManager.argument("txn_id", StringArgumentType.word())
                            .executes(TxnHandlers::closeTxn)))
                )
        );
    }
}
