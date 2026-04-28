package com.zachd.claudemod;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.Collection;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.mojang.brigadier.CommandDispatcher;
import com.mojang.brigadier.arguments.IntegerArgumentType;
import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.context.CommandContext;

import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.block.BlockState;
import net.minecraft.block.Block;
import net.minecraft.block.Blocks;
import net.minecraft.block.ChestBlock;
import net.minecraft.block.entity.BarrelBlockEntity;
import net.minecraft.block.entity.BlockEntity;
import net.minecraft.block.entity.ChestBlockEntity;
import net.minecraft.block.entity.DispenserBlockEntity;
import net.minecraft.block.entity.DropperBlockEntity;
import net.minecraft.block.entity.HopperBlockEntity;
import net.minecraft.block.entity.LootableContainerBlockEntity;
import net.minecraft.block.entity.ShulkerBoxBlockEntity;
import net.minecraft.command.argument.IdentifierArgumentType;
import net.minecraft.entity.Entity;
import net.minecraft.entity.player.PlayerInventory;
import net.minecraft.entity.vehicle.HopperMinecartEntity;
import net.minecraft.inventory.Inventory;
import net.minecraft.item.ItemStack;
import net.minecraft.nbt.NbtCompound;
import net.minecraft.nbt.NbtIo;
import net.minecraft.registry.Registries;
import net.minecraft.registry.RegistryKey;
import net.minecraft.registry.RegistryKeys;
import net.minecraft.screen.ScreenHandler;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.command.CommandManager;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.text.Text;
import net.minecraft.util.Identifier;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Box;
import net.minecraft.util.math.Direction;
import net.minecraft.util.math.Vec3d;

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
 * Slot indexing for `inventory` is dense 0..26 mapped to PlayerInventory
 * slots 9..35 — hotbar (0..8), armor, off-hand are intentionally
 * unreachable from this protocol.
 *
 * Slot indexing for `backpack_*` is dense 0..N-1 over the wearable's
 * storage handler only — tools, upgrades, fluid tanks are unreachable.
 */
public final class ClaudeWriteCommand {
    private ClaudeWriteCommand() {}

    private static final Gson GSON = new GsonBuilder().disableHtmlEscaping().create();
    private static final int MAX_RESPONSE_CHARS = 3900;

    // Tunable distance cap (squared = 64). 8-block reach matches what a
    // player can plausibly interact with at arm's length given the
    // vanilla 5-block limit + a small grace margin.
    static final double WRITE_RADIUS = 8.0;
    private static final double WRITE_RADIUS_SQ = WRITE_RADIUS * WRITE_RADIUS;

    // Inventory slots writable by the protocol (vanilla PlayerInventory).
    // Slots 0..8 = hotbar, 9..35 = main, 36..39 = armor, 40 = offhand.
    private static final int INV_RAW_MIN = 9;
    private static final int INV_RAW_MAX = 35; // inclusive
    private static final int INV_DENSE_SIZE = INV_RAW_MAX - INV_RAW_MIN + 1; // 27

    // Per-txn TTL.
    private static final long TXN_TTL_MS = 60_000;

    // Compact NBT keys we treat as volatile when computing the conservation
    // hash. Stripping these keeps a tool that ticked durability between
    // preview and commit from causing a spurious mismatch — the contents-hash
    // (separate, full-NBT) still rejects any change at TOCTOU time.
    // Top-level keys (stack root) and tag-level keys both checked.
    private static final Set<String> VOLATILE_TOP = Set.of("Damage", "RepairCost");
    private static final Set<String> VOLATILE_TAG = Set.of(
        "Damage", "RepairCost", "UUID", "UUIDLeast", "UUIDMost"
    );

    // ----- transactions -----------------------------------------------------

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
    enum Mode { DEFAULT, RESTORE, MULTI }
    enum Kind { CONTAINER, INVENTORY, BACKPACK_EQUIPPED, BACKPACK_WORLD }

    // Source RCON request payloads top out around 1413 bytes; responses
    // around 4096. We leave generous margin for command/response framing
    // ("claudemod write txn_slot_part <19-char id> <slot> <idx> <b64>" is
    // ~50 chars of overhead).
    static final int READ_NBT_INLINE_MAX = 2500;   // if b64 > this, the mod
                                                    // returns chunked metadata
                                                    // and the bridge fetches
                                                    // via read_slot_part
    static final int READ_CHUNK_SIZE = 1500;       // per-chunk slice on read
                                                    // responses
    // (write-side chunk size is the bridge's call — it picks 900 by default)

    static final class PendingTxn {
        final String id;
        final String caller;
        final Kind kind;
        final boolean isWrite;
        final Mode mode;
        final Identifier dim;        // null for inventory / backpack_equipped
        final BlockPos pos;          // null for inventory / backpack_equipped
        final int totalSlots;
        final String openContentsHash;       // hash at open time
        final List<ItemStack> openSnapshot;  // captured at open, used as snapshot if commit succeeds
        final Map<Integer, ItemStack> layout = new HashMap<>(); // for write txns
        // Per-slot full base64 cache for chunked reads — populated lazily on
        // first read_slot or read_slot_part for the slot.
        final Map<Integer, String> readB64Cache = new HashMap<>();
        // Per-slot fragment buffer for chunked writes. Keyed by chunk index
        // so out-of-order delivery is handled.
        final Map<Integer, Map<Integer, String>> writeFragments = new HashMap<>();
        long expiresAt;

        PendingTxn(String id, String caller, Kind kind, boolean isWrite, Mode mode,
                   Identifier dim, BlockPos pos, int totalSlots,
                   String openContentsHash, List<ItemStack> openSnapshot) {
            this.id = id;
            this.caller = caller;
            this.kind = kind;
            this.isWrite = isWrite;
            this.mode = mode;
            this.dim = dim;
            this.pos = pos;
            this.totalSlots = totalSlots;
            this.openContentsHash = openContentsHash;
            this.openSnapshot = openSnapshot;
            this.expiresAt = System.currentTimeMillis() + TXN_TTL_MS;
        }

        boolean expired(long now) { return now > expiresAt; }
    }

    static final Map<String, PendingTxn> TXNS = new ConcurrentHashMap<>();

    /** Periodic cleanup; called from ClaudeMod's tick handler. */
    public static void sweep() {
        long now = System.currentTimeMillis();
        TXNS.entrySet().removeIf(e -> e.getValue().expired(now));
    }

    // ----- registration -----------------------------------------------------

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
                                                .executes(ctx -> openTxn(ctx, Kind.CONTAINER, false, Mode.DEFAULT, true))))))))
                        .then(CommandManager.literal("inventory")
                            .then(CommandManager.argument("caller", StringArgumentType.word())
                                .executes(ctx -> openTxn(ctx, Kind.INVENTORY, false, Mode.DEFAULT, false))))
                        .then(CommandManager.literal("backpack_equipped")
                            .then(CommandManager.argument("caller", StringArgumentType.word())
                                .executes(ctx -> openTxn(ctx, Kind.BACKPACK_EQUIPPED, false, Mode.DEFAULT, false))))
                        .then(CommandManager.literal("backpack_world")
                            .then(CommandManager.argument("caller", StringArgumentType.word())
                                .then(CommandManager.argument("dim", IdentifierArgumentType.identifier())
                                    .then(CommandManager.argument("x", IntegerArgumentType.integer())
                                        .then(CommandManager.argument("y", IntegerArgumentType.integer())
                                            .then(CommandManager.argument("z", IntegerArgumentType.integer())
                                                .executes(ctx -> openTxn(ctx, Kind.BACKPACK_WORLD, false, Mode.DEFAULT, true))))))))
                    )
                    // ----- read_slot -----
                    .then(CommandManager.literal("read_slot")
                        .then(CommandManager.argument("txn_id", StringArgumentType.word())
                            .then(CommandManager.argument("slot", IntegerArgumentType.integer(0))
                                .executes(ClaudeWriteCommand::readSlot))))
                    // ----- read_slot_part (chunked read for large NBT) -----
                    .then(CommandManager.literal("read_slot_part")
                        .then(CommandManager.argument("txn_id", StringArgumentType.word())
                            .then(CommandManager.argument("slot", IntegerArgumentType.integer(0))
                                .then(CommandManager.argument("chunk_idx", IntegerArgumentType.integer(0))
                                    .executes(ClaudeWriteCommand::readSlotPart)))))
                    // ----- read_close -----
                    .then(CommandManager.literal("read_close")
                        .then(CommandManager.argument("txn_id", StringArgumentType.word())
                            .executes(ClaudeWriteCommand::closeTxn)))

                    // ----- txn_open (write) -----
                    .then(CommandManager.literal("txn_open")
                        .then(CommandManager.literal("container")
                            .then(CommandManager.argument("caller", StringArgumentType.word())
                                .then(CommandManager.argument("dim", IdentifierArgumentType.identifier())
                                    .then(CommandManager.argument("x", IntegerArgumentType.integer())
                                        .then(CommandManager.argument("y", IntegerArgumentType.integer())
                                            .then(CommandManager.argument("z", IntegerArgumentType.integer())
                                                .then(CommandManager.argument("hash", StringArgumentType.word())
                                                    .executes(ctx -> openTxn(ctx, Kind.CONTAINER, true, Mode.DEFAULT, true))
                                                    .then(CommandManager.argument("mode", StringArgumentType.word())
                                                        .executes(ctx -> openTxn(ctx, Kind.CONTAINER, true, parseMode(ctx), true))))))))))
                        .then(CommandManager.literal("inventory")
                            .then(CommandManager.argument("caller", StringArgumentType.word())
                                .then(CommandManager.argument("hash", StringArgumentType.word())
                                    .executes(ctx -> openTxn(ctx, Kind.INVENTORY, true, Mode.DEFAULT, false))
                                    .then(CommandManager.argument("mode", StringArgumentType.word())
                                        .executes(ctx -> openTxn(ctx, Kind.INVENTORY, true, parseMode(ctx), false))))))
                        .then(CommandManager.literal("backpack_equipped")
                            .then(CommandManager.argument("caller", StringArgumentType.word())
                                .then(CommandManager.argument("hash", StringArgumentType.word())
                                    .executes(ctx -> openTxn(ctx, Kind.BACKPACK_EQUIPPED, true, Mode.DEFAULT, false))
                                    .then(CommandManager.argument("mode", StringArgumentType.word())
                                        .executes(ctx -> openTxn(ctx, Kind.BACKPACK_EQUIPPED, true, parseMode(ctx), false))))))
                        .then(CommandManager.literal("backpack_world")
                            .then(CommandManager.argument("caller", StringArgumentType.word())
                                .then(CommandManager.argument("dim", IdentifierArgumentType.identifier())
                                    .then(CommandManager.argument("x", IntegerArgumentType.integer())
                                        .then(CommandManager.argument("y", IntegerArgumentType.integer())
                                            .then(CommandManager.argument("z", IntegerArgumentType.integer())
                                                .then(CommandManager.argument("hash", StringArgumentType.word())
                                                    .executes(ctx -> openTxn(ctx, Kind.BACKPACK_WORLD, true, Mode.DEFAULT, true))
                                                    .then(CommandManager.argument("mode", StringArgumentType.word())
                                                        .executes(ctx -> openTxn(ctx, Kind.BACKPACK_WORLD, true, parseMode(ctx), true))))))))))
                    )
                    // ----- txn_slot (single-shot, small NBT) -----
                    .then(CommandManager.literal("txn_slot")
                        .then(CommandManager.argument("txn_id", StringArgumentType.word())
                            .then(CommandManager.argument("slot", IntegerArgumentType.integer(0))
                                .then(CommandManager.argument("stack", StringArgumentType.greedyString())
                                    .executes(ClaudeWriteCommand::txnSlot)))))
                    // ----- txn_slot_part (chunked write — append a fragment) -----
                    .then(CommandManager.literal("txn_slot_part")
                        .then(CommandManager.argument("txn_id", StringArgumentType.word())
                            .then(CommandManager.argument("slot", IntegerArgumentType.integer(0))
                                .then(CommandManager.argument("chunk_idx", IntegerArgumentType.integer(0))
                                    .then(CommandManager.argument("part", StringArgumentType.greedyString())
                                        .executes(ClaudeWriteCommand::txnSlotPart))))))
                    // ----- txn_slot_finish (chunked write — finalize, build stack) -----
                    .then(CommandManager.literal("txn_slot_finish")
                        .then(CommandManager.argument("txn_id", StringArgumentType.word())
                            .then(CommandManager.argument("slot", IntegerArgumentType.integer(0))
                                .then(CommandManager.argument("count", IntegerArgumentType.integer(1))
                                    .executes(ClaudeWriteCommand::txnSlotFinish)))))
                    // ----- txn_commit -----
                    .then(CommandManager.literal("txn_commit")
                        .then(CommandManager.argument("txn_id", StringArgumentType.word())
                            .executes(ClaudeWriteCommand::txnCommit)))
                    // ----- txn_abort -----
                    .then(CommandManager.literal("txn_abort")
                        .then(CommandManager.argument("txn_id", StringArgumentType.word())
                            .executes(ClaudeWriteCommand::closeTxn)))
                )
        );
    }

    private static Mode parseMode(CommandContext<ServerCommandSource> ctx) {
        try {
            String m = StringArgumentType.getString(ctx, "mode").toLowerCase(Locale.ROOT);
            return switch (m) {
                case "restore" -> Mode.RESTORE;
                case "multi" -> Mode.MULTI;
                default -> Mode.DEFAULT;
            };
        } catch (IllegalArgumentException e) {
            return Mode.DEFAULT;
        }
    }

    // ----- handlers ---------------------------------------------------------

    private static int openTxn(CommandContext<ServerCommandSource> ctx, Kind kind, boolean isWrite,
                               Mode mode, boolean hasCoords) {
        String caller = StringArgumentType.getString(ctx, "caller");
        MinecraftServer server = ctx.getSource().getServer();
        ServerPlayerEntity callerPlayer = server.getPlayerManager().getPlayer(caller);
        if (callerPlayer == null) return errOf(ctx, "caller_offline", "caller not online: " + caller);

        Identifier dim = null;
        BlockPos pos = null;
        ServerWorld world = null;
        if (hasCoords) {
            dim = IdentifierArgumentType.getIdentifier(ctx, "dim");
            int x = IntegerArgumentType.getInteger(ctx, "x");
            int y = IntegerArgumentType.getInteger(ctx, "y");
            int z = IntegerArgumentType.getInteger(ctx, "z");
            pos = new BlockPos(x, y, z);
            world = server.getWorld(RegistryKey.of(RegistryKeys.WORLD, dim));
            if (world == null) return errOf(ctx, "bad_dim", "no world for dim: " + dim);
        }

        // Resolve inventory + accept criteria
        ResolvedTarget rt;
        try {
            rt = resolveTarget(server, callerPlayer, kind, world, pos);
        } catch (TargetException te) {
            return errOf(ctx, te.code, te.getMessage());
        }

        // Distance check (skipped for inventory/backpack_equipped — caller IS the target).
        if (kind == Kind.CONTAINER || kind == Kind.BACKPACK_WORLD) {
            // Caller must be within radius of the closest target block.
            // For double chests, rt.allPositions has both halves.
            Vec3d callerPos = callerPlayer.getPos();
            // Compare to caller's dimension if container is in a different dim.
            if (!callerPlayer.getWorld().getRegistryKey().getValue().equals(dim)) {
                return errOf(ctx, "out_of_range", "caller not in target dimension");
            }
            double minSq = Double.MAX_VALUE;
            for (BlockPos bp : rt.allPositions) {
                Vec3d center = Vec3d.ofCenter(bp);
                double sq = callerPos.squaredDistanceTo(center);
                if (sq < minSq) minSq = sq;
            }
            if (minSq > WRITE_RADIUS_SQ) {
                JsonObject extra = new JsonObject();
                extra.addProperty("distance", Math.sqrt(minSq));
                extra.addProperty("max", WRITE_RADIUS);
                return errOf(ctx, "out_of_range", "caller too far from target", extra);
            }
        }

        // Hoppers (skip for inventory, backpack_equipped, and restore mode).
        if ((kind == Kind.CONTAINER || kind == Kind.BACKPACK_WORLD) && mode != Mode.RESTORE) {
            JsonArray hoppers = detectAttachedHoppers(world, rt.allPositions);
            if (hoppers.size() > 0) {
                JsonObject extra = new JsonObject();
                extra.add("hoppers_attached", hoppers);
                return errOf(ctx, "hoppers_attached", "container has hoppers/droppers attached", extra);
            }
        }

        // Viewer guard.
        List<String> viewers = findInventoryViewers(server, rt.viewerInventories);
        if (!viewers.isEmpty()) {
            JsonObject extra = new JsonObject();
            JsonArray va = new JsonArray();
            viewers.forEach(va::add);
            extra.add("viewers", va);
            return errOf(ctx, "container_in_use", "container is being viewed", extra);
        }

        // Capture current contents (dense slot space) and compute hashes.
        List<ItemStack> snapshot = new ArrayList<>(rt.totalSlots);
        for (int i = 0; i < rt.totalSlots; i++) {
            int raw = rt.toRaw(i);
            ItemStack s = rt.inv.getStack(raw);
            snapshot.add(s == null ? ItemStack.EMPTY : s.copy());
        }
        String fullHash = hashContents(snapshot, false);

        if (isWrite) {
            String expected = StringArgumentType.getString(ctx, "hash");
            // Restore mode rebuilds a previous layout from a snapshot; the
            // expected_hash is the *pre-commit* hash, which by definition
            // doesn't match the live (post-commit) contents. Skip the gate.
            if (mode != Mode.RESTORE && !fullHash.equalsIgnoreCase(expected)) {
                JsonObject extra = new JsonObject();
                extra.addProperty("expected", expected);
                extra.addProperty("actual", fullHash);
                return errOf(ctx, "stale_txn", "contents changed since preview", extra);
            }
        }

        String id = newTxnId();
        PendingTxn t = new PendingTxn(id, caller, kind, isWrite, mode, dim, pos,
                                      rt.totalSlots, fullHash, snapshot);
        TXNS.put(id, t);

        JsonObject out = new JsonObject();
        out.addProperty("ok", true);
        out.addProperty("txn_id", id);
        out.addProperty("kind", kind.name().toLowerCase(Locale.ROOT));
        if (rt.half != null) out.addProperty("half", rt.half);
        out.addProperty("total_slots", rt.totalSlots);
        out.addProperty("contents_hash", fullHash);
        return ClaudeQueryCommand.reply(ctx, out);
    }

    private static int readSlot(CommandContext<ServerCommandSource> ctx) {
        String id = StringArgumentType.getString(ctx, "txn_id");
        int slot = IntegerArgumentType.getInteger(ctx, "slot");
        PendingTxn t = TXNS.get(id);
        if (t == null) return errOf(ctx, "unknown_txn", "no such txn_id (or expired)");
        if (t.isWrite) return errOf(ctx, "wrong_txn_kind", "this txn was opened for write, not read");
        if (slot >= t.totalSlots) return errOf(ctx, "bad_slot", "slot out of range");
        ItemStack s = t.openSnapshot.get(slot);
        JsonObject out = new JsonObject();
        if (s == null || s.isEmpty()) {
            out.addProperty("empty", true);
            return ClaudeQueryCommand.reply(ctx, out);
        }
        Identifier iid = Registries.ITEM.getId(s.getItem());
        out.addProperty("i", iid == null ? "?" : iid.toString());
        out.addProperty("c", s.getCount());
        String b64 = stackToB64(s);
        if (b64 == null) {
            // Encoding failed; bridge will see no `n` and reject on commit.
            return ClaudeQueryCommand.reply(ctx, out);
        }
        if (b64.length() <= READ_NBT_INLINE_MAX) {
            out.addProperty("n", b64);
        } else {
            // Item NBT exceeds a safe single-response budget. Cache the
            // full base64 and report chunk metadata; bridge fetches via
            // read_slot_part.
            t.readB64Cache.put(slot, b64);
            int chunks = (b64.length() + READ_CHUNK_SIZE - 1) / READ_CHUNK_SIZE;
            out.addProperty("n_size", b64.length());
            out.addProperty("n_chunks", chunks);
            out.addProperty("n_chunk_size", READ_CHUNK_SIZE);
        }
        return ClaudeQueryCommand.reply(ctx, out);
    }

    private static int readSlotPart(CommandContext<ServerCommandSource> ctx) {
        String id = StringArgumentType.getString(ctx, "txn_id");
        int slot = IntegerArgumentType.getInteger(ctx, "slot");
        int idx = IntegerArgumentType.getInteger(ctx, "chunk_idx");
        PendingTxn t = TXNS.get(id);
        if (t == null) return errOf(ctx, "unknown_txn", "no such txn_id (or expired)");
        if (t.isWrite) return errOf(ctx, "wrong_txn_kind", "this txn was opened for write, not read");
        if (slot >= t.totalSlots) return errOf(ctx, "bad_slot", "slot out of range");
        String b64 = t.readB64Cache.get(slot);
        if (b64 == null) {
            // Bridge calling read_slot_part without a prior chunked read_slot
            // — populate the cache lazily so callers can skip read_slot when
            // they already know the slot is large.
            ItemStack s = t.openSnapshot.get(slot);
            if (s == null || s.isEmpty()) return errOf(ctx, "empty_slot", "slot is empty");
            b64 = stackToB64(s);
            if (b64 == null) return errOf(ctx, "bad_stack", "stack encode failed");
            t.readB64Cache.put(slot, b64);
        }
        int start = idx * READ_CHUNK_SIZE;
        if (start >= b64.length() || idx < 0) return errOf(ctx, "bad_chunk",
            "chunk_idx out of range");
        int end = Math.min(start + READ_CHUNK_SIZE, b64.length());
        JsonObject out = new JsonObject();
        out.addProperty("part", b64.substring(start, end));
        out.addProperty("done", end == b64.length());
        return ClaudeQueryCommand.reply(ctx, out);
    }

    private static int txnSlotPart(CommandContext<ServerCommandSource> ctx) {
        String id = StringArgumentType.getString(ctx, "txn_id");
        int slot = IntegerArgumentType.getInteger(ctx, "slot");
        int idx = IntegerArgumentType.getInteger(ctx, "chunk_idx");
        String part = StringArgumentType.getString(ctx, "part").trim();
        PendingTxn t = TXNS.get(id);
        if (t == null) return errOf(ctx, "unknown_txn", "no such txn_id (or expired)");
        if (!t.isWrite) return errOf(ctx, "wrong_txn_kind", "this txn was opened for read, not write");
        if (slot < 0 || slot >= t.totalSlots) return errOf(ctx, "bad_slot",
            "slot out of range [0, " + t.totalSlots + ")");
        Map<Integer, String> frags = t.writeFragments.computeIfAbsent(slot, k -> new HashMap<>());
        frags.put(idx, part);
        t.expiresAt = System.currentTimeMillis() + TXN_TTL_MS;
        JsonObject out = new JsonObject();
        out.addProperty("ok", true);
        return ClaudeQueryCommand.reply(ctx, out);
    }

    private static int txnSlotFinish(CommandContext<ServerCommandSource> ctx) {
        String id = StringArgumentType.getString(ctx, "txn_id");
        int slot = IntegerArgumentType.getInteger(ctx, "slot");
        int count = IntegerArgumentType.getInteger(ctx, "count");
        PendingTxn t = TXNS.get(id);
        if (t == null) return errOf(ctx, "unknown_txn", "no such txn_id (or expired)");
        if (!t.isWrite) return errOf(ctx, "wrong_txn_kind", "this txn was opened for read, not write");
        if (slot < 0 || slot >= t.totalSlots) return errOf(ctx, "bad_slot",
            "slot out of range [0, " + t.totalSlots + ")");
        if (count < 1) return errOf(ctx, "bad_stack", "count must be >= 1");
        Map<Integer, String> frags = t.writeFragments.remove(slot);
        if (frags == null || frags.isEmpty()) {
            return errOf(ctx, "no_fragments", "no chunked fragments staged for this slot");
        }
        // Concatenate in chunk-index order.
        int[] indices = frags.keySet().stream().mapToInt(Integer::intValue).sorted().toArray();
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < indices.length; i++) {
            if (indices[i] != i) {
                return errOf(ctx, "missing_chunk",
                    "expected chunk_idx " + i + " but next received was " + indices[i]);
            }
            sb.append(frags.get(indices[i]));
        }
        ItemStack stack;
        try {
            stack = stackFromB64(sb.toString());
            stack.setCount(count);
        } catch (Exception e) {
            return errOf(ctx, "bad_stack", "stack decode failed: " + e.getMessage());
        }
        t.layout.put(slot, stack);
        t.expiresAt = System.currentTimeMillis() + TXN_TTL_MS;
        JsonObject out = new JsonObject();
        out.addProperty("ok", true);
        return ClaudeQueryCommand.reply(ctx, out);
    }

    private static int closeTxn(CommandContext<ServerCommandSource> ctx) {
        String id = StringArgumentType.getString(ctx, "txn_id");
        PendingTxn t = TXNS.remove(id);
        JsonObject out = new JsonObject();
        out.addProperty("ok", t != null);
        return ClaudeQueryCommand.reply(ctx, out);
    }

    private static int txnSlot(CommandContext<ServerCommandSource> ctx) {
        String id = StringArgumentType.getString(ctx, "txn_id");
        int slot = IntegerArgumentType.getInteger(ctx, "slot");
        String stackArg = StringArgumentType.getString(ctx, "stack").trim();
        PendingTxn t = TXNS.get(id);
        if (t == null) return errOf(ctx, "unknown_txn", "no such txn_id (or expired)");
        if (!t.isWrite) return errOf(ctx, "wrong_txn_kind", "this txn was opened for read, not write");
        if (slot < 0 || slot >= t.totalSlots) return errOf(ctx, "bad_slot",
            "slot out of range [0, " + t.totalSlots + ")");

        ItemStack stack;
        if (stackArg.equals("-") || stackArg.equals("_empty_") || stackArg.isEmpty()) {
            stack = ItemStack.EMPTY;
        } else {
            // Wire format: "<count>:<base64-nbt>" — count is supplied separately
            // from the NBT so the bridge can merge multiple source stacks into
            // one target slot (sum of source counts) without re-encoding NBT
            // bytes. Empty stacks use the literal "-" form above.
            int colon = stackArg.indexOf(':');
            if (colon < 0) {
                return errOf(ctx, "bad_stack", "expected '<count>:<base64>' or '-' for empty");
            }
            try {
                int count = Integer.parseInt(stackArg.substring(0, colon));
                if (count < 1) return errOf(ctx, "bad_stack", "count must be >= 1");
                stack = stackFromB64(stackArg.substring(colon + 1));
                stack.setCount(count);
            } catch (Exception e) {
                return errOf(ctx, "bad_stack", "stack decode failed: " + e.getMessage());
            }
        }
        t.layout.put(slot, stack);
        t.expiresAt = System.currentTimeMillis() + TXN_TTL_MS; // bump

        JsonObject out = new JsonObject();
        out.addProperty("ok", true);
        return ClaudeQueryCommand.reply(ctx, out);
    }

    private static int txnCommit(CommandContext<ServerCommandSource> ctx) {
        String id = StringArgumentType.getString(ctx, "txn_id");
        PendingTxn t = TXNS.get(id);
        if (t == null) return errOf(ctx, "unknown_txn", "no such txn_id (or expired)");
        if (!t.isWrite) return errOf(ctx, "wrong_txn_kind", "this txn was opened for read, not write");

        MinecraftServer server = ctx.getSource().getServer();
        ServerPlayerEntity callerPlayer = server.getPlayerManager().getPlayer(t.caller);
        if (callerPlayer == null) {
            TXNS.remove(id);
            return errOf(ctx, "caller_offline", "caller went offline");
        }

        ServerWorld world = null;
        if (t.dim != null) {
            world = server.getWorld(RegistryKey.of(RegistryKeys.WORLD, t.dim));
            if (world == null) {
                TXNS.remove(id);
                return errOf(ctx, "bad_dim", "world unloaded");
            }
        }

        // Re-resolve target on the live world (BlockEntity may have been
        // replaced; backpack may have been unequipped).
        ResolvedTarget rt;
        try {
            rt = resolveTarget(server, callerPlayer, t.kind, world, t.pos);
        } catch (TargetException te) {
            TXNS.remove(id);
            return errOf(ctx, te.code, te.getMessage());
        }
        if (rt.totalSlots != t.totalSlots) {
            TXNS.remove(id);
            return errOf(ctx, "shape_changed", "slot count changed since open");
        }

        // Re-check viewer guard at commit time (TOCTOU — someone may have
        // opened the chest between txn_open and txn_commit).
        List<String> viewers = findInventoryViewers(server, rt.viewerInventories);
        if (!viewers.isEmpty()) {
            JsonObject extra = new JsonObject();
            JsonArray va = new JsonArray();
            viewers.forEach(va::add);
            extra.add("viewers", va);
            return errOf(ctx, "container_in_use", "container is being viewed", extra);
        }

        // Re-check distance & re-snapshot live contents.
        if (t.kind == Kind.CONTAINER || t.kind == Kind.BACKPACK_WORLD) {
            Vec3d cp = callerPlayer.getPos();
            if (!callerPlayer.getWorld().getRegistryKey().getValue().equals(t.dim)) {
                return errOf(ctx, "out_of_range", "caller not in target dimension");
            }
            double minSq = Double.MAX_VALUE;
            for (BlockPos bp : rt.allPositions) {
                double sq = cp.squaredDistanceTo(Vec3d.ofCenter(bp));
                if (sq < minSq) minSq = sq;
            }
            if (minSq > WRITE_RADIUS_SQ) {
                return errOf(ctx, "out_of_range", "caller drifted out of range");
            }
        }

        List<ItemStack> live = new ArrayList<>(t.totalSlots);
        for (int i = 0; i < t.totalSlots; i++) {
            int raw = rt.toRaw(i);
            ItemStack s = rt.inv.getStack(raw);
            live.add(s == null ? ItemStack.EMPTY : s.copy());
        }
        String liveHash = hashContents(live, false);
        if (!liveHash.equalsIgnoreCase(t.openContentsHash)) {
            JsonObject extra = new JsonObject();
            extra.addProperty("expected", t.openContentsHash);
            extra.addProperty("actual", liveHash);
            TXNS.remove(id);
            return errOf(ctx, "stale_txn", "contents changed during txn", extra);
        }

        // Build proposed layout (slots not addressed = empty).
        List<ItemStack> proposed = new ArrayList<>(t.totalSlots);
        for (int i = 0; i < t.totalSlots; i++) {
            ItemStack s = t.layout.get(i);
            proposed.add(s == null ? ItemStack.EMPTY : s);
        }

        // Item conservation (stripped NBT) — only run in DEFAULT mode.
        // RESTORE trusts the snapshot; MULTI defers to bridge-side cross-target
        // conservation (per-target multisets don't balance when items move
        // between containers).
        if (t.mode == Mode.DEFAULT) {
            String mismatch = checkConservation(live, proposed);
            if (mismatch != null) {
                JsonObject extra = new JsonObject();
                extra.addProperty("detail", mismatch);
                TXNS.remove(id);
                return errOf(ctx, "conservation_failed", "item conservation violated", extra);
            }
        }

        // Apply atomically (single tick, command thread).
        for (int i = 0; i < t.totalSlots; i++) {
            int raw = rt.toRaw(i);
            rt.inv.setStack(raw, proposed.get(i));
        }
        rt.markChanged(world);

        // Ring-buffer of recent commits, keyed by caller. Bridge persists
        // the snapshot to PVC; mod returns the old contents inline so the
        // bridge has authoritative pre-state.
        TXNS.remove(id);

        JsonObject out = new JsonObject();
        out.addProperty("ok", true);
        out.addProperty("applied", t.totalSlots);
        out.addProperty("snapshot_pre_hash", t.openContentsHash);
        return ClaudeQueryCommand.reply(ctx, out);
    }

    // ----- target resolution ------------------------------------------------

    /** Resolved view of the target inventory + metadata used for safety checks. */
    private static final class ResolvedTarget {
        final Inventory inv;
        final int totalSlots;            // dense slot count exposed to protocol
        final String half;               // "left" / "right" / "single" / null
        final List<BlockPos> allPositions; // for distance + hopper check; empty for non-block targets
        final List<Inventory> viewerInventories; // inventories to check viewers against
        final int rawOffset;             // for inventory: 9 (so dense 0 -> raw 9)
        final int[] rawMap;              // for backpack: sparse mapping (storage slots only)
        final Runnable markChange;       // hook to call markDirty()/updateListeners()
        final Kind kind;

        ResolvedTarget(Inventory inv, int total, String half,
                       List<BlockPos> allPositions, List<Inventory> viewerInvs,
                       int rawOffset, int[] rawMap, Runnable markChange, Kind kind) {
            this.inv = inv;
            this.totalSlots = total;
            this.half = half;
            this.allPositions = allPositions;
            this.viewerInventories = viewerInvs;
            this.rawOffset = rawOffset;
            this.rawMap = rawMap;
            this.markChange = markChange;
            this.kind = kind;
        }

        int toRaw(int dense) {
            if (rawMap != null) return rawMap[dense];
            return dense + rawOffset;
        }

        void markChanged(ServerWorld world) {
            try { markChange.run(); } catch (Throwable t) {
                ClaudeMod.LOG.warn("markChanged failed: {}", t.toString());
            }
            if (!allPositions.isEmpty() && world != null) {
                for (BlockPos bp : allPositions) {
                    BlockState st = world.getBlockState(bp);
                    world.updateListeners(bp, st, st, Block.NOTIFY_LISTENERS);
                }
            }
        }
    }

    private static final class TargetException extends RuntimeException {
        final String code;
        TargetException(String code, String msg) { super(msg); this.code = code; }
    }

    private static ResolvedTarget resolveTarget(MinecraftServer server,
                                                ServerPlayerEntity caller,
                                                Kind kind,
                                                ServerWorld world,
                                                BlockPos pos) {
        switch (kind) {
            case INVENTORY: {
                PlayerInventory pinv = caller.getInventory();
                Runnable mark = pinv::markDirty;
                return new ResolvedTarget(pinv, INV_DENSE_SIZE, null,
                    List.of(), List.of(pinv), INV_RAW_MIN, null, mark, kind);
            }
            case CONTAINER: {
                BlockEntity be = world.getBlockEntity(pos);
                if (be == null) throw new TargetException("wrong_target_type", "no block entity at " + pos.toShortString());
                if (be instanceof HopperBlockEntity || be instanceof DispenserBlockEntity || be instanceof DropperBlockEntity) {
                    throw new TargetException("wrong_target_type", "this container kind is not writable: " + be.getType());
                }
                if (be instanceof ChestBlockEntity) {
                    BlockState st = world.getBlockState(pos);
                    if (!(st.getBlock() instanceof ChestBlock cb)) {
                        throw new TargetException("wrong_target_type", "expected a chest block");
                    }
                    Inventory dbl = ChestBlock.getInventory(cb, st, world, pos, true);
                    if (dbl == null) throw new TargetException("wrong_target_type", "could not resolve chest inventory");
                    BlockPos paired = pairedChestPos(world, pos, st);
                    String half = paired == null ? "single" : (pos.compareTo(paired) < 0 ? "left" : "right");
                    List<BlockPos> all = paired == null ? List.of(pos) : List.of(pos, paired);
                    List<Inventory> viewers = new ArrayList<>();
                    viewers.add((Inventory) be);
                    if (paired != null) {
                        BlockEntity be2 = world.getBlockEntity(paired);
                        if (be2 instanceof Inventory inv2) viewers.add(inv2);
                    }
                    Inventory inv = dbl;
                    Runnable mark = () -> {
                        be.markDirty();
                        if (paired != null) {
                            BlockEntity be2 = world.getBlockEntity(paired);
                            if (be2 != null) be2.markDirty();
                        }
                    };
                    return new ResolvedTarget(inv, inv.size(), half, all, viewers, 0, null, mark, kind);
                }
                if (be instanceof BarrelBlockEntity || be instanceof ShulkerBoxBlockEntity) {
                    Inventory inv = (Inventory) be;
                    Runnable mark = be::markDirty;
                    return new ResolvedTarget(inv, inv.size(), "single",
                        List.of(pos), List.of(inv), 0, null, mark, kind);
                }
                throw new TargetException("wrong_target_type",
                    "container kind not in allowlist (chest/barrel/shulker): " + be.getType());
            }
            case BACKPACK_EQUIPPED: {
                if (!FabricLoader.getInstance().isModLoaded("travelersbackpack")) {
                    throw new TargetException("backpack_unsupported", "travelersbackpack mod not loaded");
                }
                Object handler = resolveBackpackStorage(caller, null);
                if (handler == null) {
                    throw new TargetException("backpack_unequipped", "caller is not wearing a backpack");
                }
                int n = handlerSize(handler);
                int[] map = new int[n];
                for (int i = 0; i < n; i++) map[i] = i;
                Inventory inv = handlerAsInventory(handler);
                Runnable mark = () -> backpackSync(caller);
                return new ResolvedTarget(inv, n, null, List.of(), List.of(inv), 0, map, mark, kind);
            }
            case BACKPACK_WORLD: {
                if (!FabricLoader.getInstance().isModLoaded("travelersbackpack")) {
                    throw new TargetException("backpack_unsupported", "travelersbackpack mod not loaded");
                }
                BlockEntity be = world.getBlockEntity(pos);
                if (be == null) throw new TargetException("wrong_target_type", "no block entity at " + pos.toShortString());
                Object handler = resolveBackpackStorage(null, be);
                if (handler == null) {
                    throw new TargetException("wrong_target_type", "block entity is not a travelersbackpack");
                }
                int n = handlerSize(handler);
                int[] map = new int[n];
                for (int i = 0; i < n; i++) map[i] = i;
                Inventory inv = handlerAsInventory(handler);
                Runnable mark = be::markDirty;
                return new ResolvedTarget(inv, n, "single", List.of(pos), List.of(inv), 0, map, mark, kind);
            }
            default:
                throw new TargetException("wrong_target_type", "unknown target kind");
        }
    }

    private static BlockPos pairedChestPos(ServerWorld world, BlockPos pos, BlockState st) {
        if (!(st.getBlock() instanceof ChestBlock)) return null;
        var ct = st.get(ChestBlock.CHEST_TYPE);
        if (ct == net.minecraft.block.enums.ChestType.SINGLE) return null;
        Direction facing = st.get(ChestBlock.FACING);
        Direction offset = ct == net.minecraft.block.enums.ChestType.LEFT
            ? facing.rotateYClockwise()
            : facing.rotateYCounterclockwise();
        return pos.offset(offset);
    }

    // ----- safety helpers ---------------------------------------------------

    private static JsonArray detectAttachedHoppers(ServerWorld world, List<BlockPos> targets) {
        JsonArray hits = new JsonArray();
        Set<BlockPos> targetSet = new HashSet<>(targets);
        // Adjacent block check (hopper / dropper / dispenser)
        for (BlockPos target : targets) {
            for (Direction d : Direction.values()) {
                BlockPos n = target.offset(d);
                if (targetSet.contains(n)) continue; // skip neighbor that IS the other half
                BlockState st = world.getBlockState(n);
                BlockEntity nbe = world.getBlockEntity(n);
                if (nbe instanceof HopperBlockEntity) {
                    Direction face = Direction.DOWN; // default
                    try {
                        face = st.get(net.minecraft.block.HopperBlock.FACING);
                    } catch (IllegalArgumentException ignore) {}
                    boolean feeds = (n.offset(face).equals(target));
                    boolean pulls = (target.up().equals(n));
                    if (feeds || pulls) {
                        JsonObject h = new JsonObject();
                        h.addProperty("type", "hopper");
                        h.addProperty("x", n.getX()); h.addProperty("y", n.getY()); h.addProperty("z", n.getZ());
                        h.addProperty("relation", feeds ? "feeds" : "pulls");
                        hits.add(h);
                    }
                }
                // Dropper / dispenser facing INTO target (could push on redstone).
                if (nbe instanceof DropperBlockEntity || nbe instanceof DispenserBlockEntity) {
                    try {
                        Direction face = st.get(net.minecraft.block.DispenserBlock.FACING);
                        if (n.offset(face).equals(target)) {
                            JsonObject h = new JsonObject();
                            h.addProperty("type", nbe instanceof DropperBlockEntity ? "dropper" : "dispenser");
                            h.addProperty("x", n.getX()); h.addProperty("y", n.getY()); h.addProperty("z", n.getZ());
                            h.addProperty("relation", "feeds");
                            hits.add(h);
                        }
                    } catch (IllegalArgumentException ignore) {}
                }
            }
        }
        // Hopper minecart in/near the target column.
        if (!targets.isEmpty()) {
            BlockPos first = targets.get(0);
            Box box = new Box(first).expand(2.0);
            for (BlockPos bp : targets) box = box.union(new Box(bp).expand(2.0));
            for (Entity e : world.getOtherEntities(null, box)) {
                if (e instanceof HopperMinecartEntity) {
                    JsonObject h = new JsonObject();
                    h.addProperty("type", "hopper_minecart");
                    h.addProperty("x", e.getBlockX());
                    h.addProperty("y", e.getBlockY());
                    h.addProperty("z", e.getBlockZ());
                    h.addProperty("relation", "nearby");
                    hits.add(h);
                }
            }
        }
        return hits;
    }

    private static List<String> findInventoryViewers(MinecraftServer server, List<Inventory> targets) {
        List<String> out = new ArrayList<>();
        if (targets.isEmpty()) return out;
        Set<Inventory> targetSet = new HashSet<>(targets);
        for (ServerPlayerEntity p : server.getPlayerManager().getPlayerList()) {
            ScreenHandler sh = p.currentScreenHandler;
            if (sh == null || sh == p.playerScreenHandler) continue;
            // Inspect the slots — if any references one of our target invs, the player has it open.
            try {
                int n = sh.slots.size();
                for (int i = 0; i < n; i++) {
                    var slot = sh.slots.get(i);
                    if (slot != null && targetSet.contains(slot.inventory)) {
                        out.add(p.getName().getString());
                        break;
                    }
                }
            } catch (Throwable t) {
                // best-effort; if a custom mod ScreenHandler has a non-standard slot list,
                // we'd rather skip it than abort the entire safety check.
            }
        }
        return out;
    }

    // ----- conservation -----------------------------------------------------

    /** Returns null on success, or a human-readable reason on mismatch. */
    private static String checkConservation(List<ItemStack> a, List<ItemStack> b) {
        Map<String, Integer> ca = countMultiset(a);
        Map<String, Integer> cb = countMultiset(b);
        if (ca.equals(cb)) return null;
        Map<String, Integer> diff = new HashMap<>();
        for (var e : ca.entrySet()) diff.merge(e.getKey(), e.getValue(), Integer::sum);
        for (var e : cb.entrySet()) diff.merge(e.getKey(), -e.getValue(), Integer::sum);
        diff.entrySet().removeIf(e -> e.getValue() == 0);
        StringBuilder sb = new StringBuilder("delta:");
        int n = 0;
        for (var e : diff.entrySet()) {
            if (n++ > 6) { sb.append(" ..."); break; }
            sb.append(' ').append(e.getKey()).append('=').append(e.getValue());
        }
        return sb.toString();
    }

    private static Map<String, Integer> countMultiset(List<ItemStack> stacks) {
        Map<String, Integer> m = new HashMap<>();
        for (ItemStack s : stacks) {
            if (s == null || s.isEmpty()) continue;
            String key = stackKey(s);
            m.merge(key, s.getCount(), Integer::sum);
        }
        return m;
    }

    /** Stripped-NBT identity key: item id + sha256(stripped(nbt)). */
    private static String stackKey(ItemStack s) {
        Identifier id = Registries.ITEM.getId(s.getItem());
        String iid = id == null ? "?" : id.toString();
        NbtCompound full = new NbtCompound();
        s.writeNbt(full);
        NbtCompound stripped = stripVolatile(full);
        // Drop count from the NBT identity — count is multiset multiplicity.
        stripped.remove("Count");
        return iid + "|" + sha256OfNbt(stripped);
    }

    private static NbtCompound stripVolatile(NbtCompound src) {
        NbtCompound out = src.copy();
        for (String k : VOLATILE_TOP) out.remove(k);
        if (out.contains("tag", 10)) {
            NbtCompound tag = out.getCompound("tag").copy();
            for (String k : VOLATILE_TAG) tag.remove(k);
            // Also: Tiered.durability — the modpack uses this for runtime
            // durability counters that can drift mid-session.
            if (tag.contains("Tiered", 10)) {
                NbtCompound tiered = tag.getCompound("Tiered").copy();
                tiered.remove("durability");
                tag.put("Tiered", tiered);
            }
            // durable (modded) — runtime durability counter.
            tag.remove("durable");
            if (tag.isEmpty()) out.remove("tag");
            else out.put("tag", tag);
        }
        return out;
    }

    // ----- hashing & encoding -----------------------------------------------

    private static String hashContents(List<ItemStack> stacks, boolean stripped) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            DataOutputStream dos = new DataOutputStream(buf);
            dos.writeInt(stacks.size());
            for (int i = 0; i < stacks.size(); i++) {
                ItemStack s = stacks.get(i);
                dos.writeInt(i);
                if (s == null || s.isEmpty()) {
                    dos.writeByte(0);
                } else {
                    dos.writeByte(1);
                    NbtCompound nbt = new NbtCompound();
                    s.writeNbt(nbt);
                    if (stripped) nbt = stripVolatile(nbt);
                    NbtIo.write(nbt, dos);
                }
            }
            md.update(buf.toByteArray());
            return toHex(md.digest());
        } catch (Exception e) {
            ClaudeMod.LOG.warn("hashContents failed: {}", e.toString());
            return "";
        }
    }

    private static String sha256OfNbt(NbtCompound c) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            NbtIo.write(c, new DataOutputStream(buf));
            md.update(buf.toByteArray());
            return toHex(md.digest()).substring(0, 16); // short prefix for compactness
        } catch (Exception e) { return "?"; }
    }

    private static String toHex(byte[] b) {
        StringBuilder sb = new StringBuilder(b.length * 2);
        for (byte x : b) sb.append(String.format("%02x", x));
        return sb.toString();
    }

    private static String stackToB64(ItemStack s) {
        try {
            NbtCompound nbt = new NbtCompound();
            s.writeNbt(nbt);
            ByteArrayOutputStream baos = new ByteArrayOutputStream();
            NbtIo.write(nbt, new DataOutputStream(baos));
            return Base64.getEncoder().withoutPadding().encodeToString(baos.toByteArray());
        } catch (Exception e) {
            ClaudeMod.LOG.warn("stackToB64 failed: {}", e.toString());
            return null;
        }
    }

    private static ItemStack stackFromB64(String b64) throws Exception {
        // Padded or unpadded both accepted.
        byte[] raw = Base64.getDecoder().decode(b64);
        NbtCompound nbt = NbtIo.read(new DataInputStream(new ByteArrayInputStream(raw)),
            net.minecraft.nbt.NbtTagSizeTracker.EMPTY);
        return ItemStack.fromNbt(nbt);
    }

    // ----- backpack reflection ---------------------------------------------

    /** Returns the storage ItemStackHandler (raw object) for an equipped or world backpack. */
    private static Object resolveBackpackStorage(ServerPlayerEntity caller, BlockEntity be) {
        try {
            if (caller != null) {
                Class<?> utils = Class.forName("com.tiviacz.travelersbackpack.component.ComponentUtils");
                Object wearing = invokeStaticOneArg(utils, "isWearingBackpack", caller);
                if (!(wearing instanceof Boolean) || !((Boolean) wearing)) return null;
                Object wrapper = invokeStaticOneArg(utils, "getBackpackWrapper", caller);
                if (wrapper == null) return null;
                for (var m : wrapper.getClass().getMethods()) {
                    if (m.getName().equals("getStorage") && m.getParameterCount() == 0) {
                        return m.invoke(wrapper);
                    }
                }
                return null;
            }
            if (be != null) {
                // World-placed backpack BE has a getInventory()/getStorage()-shaped accessor.
                // Iterate the BE's class methods looking for one returning an ItemStackHandler-like object.
                for (var m : be.getClass().getMethods()) {
                    if (m.getParameterCount() != 0) continue;
                    String n = m.getName();
                    if (n.equals("getStorage") || n.equals("getInventory") || n.equals("getStorageInventory")) {
                        Object h = m.invoke(be);
                        if (h != null && hasMethod(h, "getSlots") && hasMethod(h, "getStackInSlot")) {
                            return h;
                        }
                    }
                }
            }
        } catch (Throwable t) {
            ClaudeMod.LOG.warn("resolveBackpackStorage failed: {}", t.toString());
        }
        return null;
    }

    private static Object invokeStaticOneArg(Class<?> cls, String method, Object arg) {
        for (var m : cls.getMethods()) {
            if (!m.getName().equals(method) || m.getParameterCount() != 1) continue;
            try {
                return m.invoke(null, arg);
            } catch (Throwable ignore) {}
        }
        return null;
    }

    private static boolean hasMethod(Object o, String name) {
        for (var m : o.getClass().getMethods()) if (m.getName().equals(name)) return true;
        return false;
    }

    private static int handlerSize(Object handler) {
        try {
            return (int) handler.getClass().getMethod("getSlots").invoke(handler);
        } catch (Throwable t) { return 0; }
    }

    /** Wrap an ItemStackHandler-like object in a vanilla Inventory facade. */
    private static Inventory handlerAsInventory(Object handler) {
        return new HandlerInventory(handler);
    }

    private static final class HandlerInventory implements Inventory {
        private final Object h;
        private final java.lang.reflect.Method get, set, slots;
        HandlerInventory(Object h) {
            this.h = h;
            try {
                this.get = h.getClass().getMethod("getStackInSlot", int.class);
                this.slots = h.getClass().getMethod("getSlots");
                java.lang.reflect.Method s = null;
                // setStackInSlot is the standard ItemStackHandler write entry point.
                for (var m : h.getClass().getMethods()) {
                    if (m.getName().equals("setStackInSlot")
                        && m.getParameterCount() == 2
                        && m.getParameterTypes()[0] == int.class) { s = m; break; }
                }
                this.set = s;
            } catch (Throwable t) {
                throw new RuntimeException("backpack handler missing required methods", t);
            }
        }
        @Override public int size() {
            try { return (int) slots.invoke(h); } catch (Throwable t) { return 0; }
        }
        @Override public boolean isEmpty() {
            int n = size();
            for (int i = 0; i < n; i++) if (!getStack(i).isEmpty()) return false;
            return true;
        }
        @Override public ItemStack getStack(int slot) {
            try { return (ItemStack) get.invoke(h, slot); } catch (Throwable t) { return ItemStack.EMPTY; }
        }
        @Override public ItemStack removeStack(int slot, int amount) {
            ItemStack s = getStack(slot);
            if (s.isEmpty()) return ItemStack.EMPTY;
            ItemStack split = s.split(amount);
            setStack(slot, s);
            return split;
        }
        @Override public ItemStack removeStack(int slot) {
            ItemStack s = getStack(slot);
            setStack(slot, ItemStack.EMPTY);
            return s;
        }
        @Override public void setStack(int slot, ItemStack stack) {
            try { if (set != null) set.invoke(h, slot, stack); }
            catch (Throwable t) { ClaudeMod.LOG.warn("backpack setStack failed: {}", t.toString()); }
        }
        @Override public void markDirty() { /* handled by mark hook */ }
        @Override public boolean canPlayerUse(net.minecraft.entity.player.PlayerEntity player) { return true; }
        @Override public void clear() {
            int n = size();
            for (int i = 0; i < n; i++) setStack(i, ItemStack.EMPTY);
        }
    }

    /** Trigger Travelers Backpack's component sync on an equipped wearable. */
    private static void backpackSync(ServerPlayerEntity caller) {
        try {
            Class<?> utils = Class.forName("com.tiviacz.travelersbackpack.component.ComponentUtils");
            // Try a few likely method names for the sync helper.
            for (String name : new String[]{"syncBackpack", "sync", "syncToClient"}) {
                for (var m : utils.getMethods()) {
                    if (m.getName().equals(name) && m.getParameterCount() == 1) {
                        try { m.invoke(null, caller); return; } catch (Throwable ignore) {}
                    }
                }
            }
            // Fallback: re-equip the wearable to force a component write.
            Object wrapper = invokeStaticOneArg(utils, "getBackpackWrapper", caller);
            if (wrapper != null) {
                for (var m : wrapper.getClass().getMethods()) {
                    if (m.getName().equals("markDirty") && m.getParameterCount() == 0) {
                        try { m.invoke(wrapper); return; } catch (Throwable ignore) {}
                    }
                }
            }
        } catch (Throwable t) {
            ClaudeMod.LOG.warn("backpackSync failed: {}", t.toString());
        }
    }

    // ----- error & misc -----------------------------------------------------

    private static int errOf(CommandContext<ServerCommandSource> ctx, String code, String msg) {
        return errOf(ctx, code, msg, null);
    }

    private static int errOf(CommandContext<ServerCommandSource> ctx, String code, String msg, JsonObject extra) {
        JsonObject o = new JsonObject();
        o.addProperty("ok", false);
        o.addProperty("error", code);
        o.addProperty("detail", msg);
        if (extra != null) {
            for (var e : extra.entrySet()) o.add(e.getKey(), e.getValue());
        }
        String json = GSON.toJson(o);
        if (json.length() > MAX_RESPONSE_CHARS) json = json.substring(0, MAX_RESPONSE_CHARS - 1);
        final String out = json;
        ctx.getSource().sendFeedback(() -> Text.literal(out), false);
        return 0;
    }

    private static String newTxnId() {
        // Sortable: ms timestamp + 8 hex chars of randomness.
        long ms = System.currentTimeMillis();
        String r = Long.toHexString(UUID.randomUUID().getLeastSignificantBits() & 0xFFFFFFFFL);
        while (r.length() < 8) r = "0" + r;
        return ms + "-" + r;
    }
}
