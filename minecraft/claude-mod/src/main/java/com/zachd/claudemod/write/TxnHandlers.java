package com.zachd.claudemod.write;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.mojang.brigadier.arguments.IntegerArgumentType;
import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.context.CommandContext;

import net.minecraft.command.argument.IdentifierArgumentType;
import net.minecraft.item.ItemStack;
import net.minecraft.registry.Registries;
import net.minecraft.registry.RegistryKey;
import net.minecraft.registry.RegistryKeys;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.text.Text;
import net.minecraft.util.Identifier;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Vec3d;

import com.zachd.claudemod.shared.ClaudeIo;

/**
 * Brigadier {@code .executes(...)} handlers for the /claudemod write protocol.
 *
 * Lifecycle:
 *   {@link #openTxn} — validates caller, distance, hoppers, viewers; captures
 *      the open-time snapshot and contents hash; returns a fresh txn id.
 *   {@link #readSlot} / {@link #readSlotPart} — chunked read, large NBT
 *      streams via cached b64 slices.
 *   {@link #txnSlot} / {@link #txnSlotPart} / {@link #txnSlotFinish} — chunked
 *      write, fragments assembled in-order on finish.
 *   {@link #txnCommit} — re-runs every safety gate (TOCTOU), checks
 *      conservation in DEFAULT mode, then atomically applies the layout.
 *   {@link #closeTxn} — read_close + txn_abort.
 */
public final class TxnHandlers {
    private TxnHandlers() {}

    // Tunable distance cap (squared = 64). 8-block reach matches what a
    // player can plausibly interact with at arm's length given the
    // vanilla 5-block limit + a small grace margin.
    public static final double WRITE_RADIUS = 8.0;
    private static final double WRITE_RADIUS_SQ = WRITE_RADIUS * WRITE_RADIUS;

    // Source RCON request payloads top out around 1413 bytes; responses
    // around 4096. We leave generous margin for command/response framing
    // ("claudemod write txn_slot_part <19-char id> <slot> <idx> <b64>" is
    // ~50 chars of overhead).
    private static final int READ_NBT_INLINE_MAX = 2500;   // if b64 > this, the mod
                                                            // returns chunked metadata
                                                            // and the bridge fetches
                                                            // via read_slot_part
    private static final int READ_CHUNK_SIZE = 1500;       // per-chunk slice on read
                                                            // responses
    // (write-side chunk size is the bridge's call — it picks 900 by default)

    public static int openTxn(CommandContext<ServerCommandSource> ctx, Kind kind, boolean isWrite,
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
            rt = TargetResolver.resolveTarget(server, callerPlayer, kind, world, pos);
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
            JsonArray hoppers = SafetyChecks.detectAttachedHoppers(world, rt.allPositions);
            if (hoppers.size() > 0) {
                JsonObject extra = new JsonObject();
                extra.add("hoppers_attached", hoppers);
                return errOf(ctx, "hoppers_attached", "container has hoppers/droppers attached", extra);
            }
        }

        // Viewer guard.
        List<String> viewers = SafetyChecks.findInventoryViewers(server, rt.viewerInventories);
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
        String fullHash = Conservation.hashContents(snapshot, false);

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

        String id = PendingTxn.newId();
        PendingTxn t = new PendingTxn(id, caller, kind, isWrite, mode, dim, pos,
                                      rt.totalSlots, fullHash, snapshot);
        PendingTxn.TXNS.put(id, t);

        JsonObject out = new JsonObject();
        out.addProperty("ok", true);
        out.addProperty("txn_id", id);
        out.addProperty("kind", kind.name().toLowerCase(Locale.ROOT));
        if (rt.half != null) out.addProperty("half", rt.half);
        out.addProperty("total_slots", rt.totalSlots);
        out.addProperty("contents_hash", fullHash);
        return ClaudeIo.reply(ctx, out);
    }

    public static int readSlot(CommandContext<ServerCommandSource> ctx) {
        String id = StringArgumentType.getString(ctx, "txn_id");
        int slot = IntegerArgumentType.getInteger(ctx, "slot");
        PendingTxn t = PendingTxn.TXNS.get(id);
        if (t == null) return errOf(ctx, "unknown_txn", "no such txn_id (or expired)");
        if (t.isWrite) return errOf(ctx, "wrong_txn_kind", "this txn was opened for write, not read");
        if (slot >= t.totalSlots) return errOf(ctx, "bad_slot", "slot out of range");
        ItemStack s = t.openSnapshot.get(slot);
        JsonObject out = new JsonObject();
        if (s == null || s.isEmpty()) {
            out.addProperty("empty", true);
            return ClaudeIo.reply(ctx, out);
        }
        Identifier iid = Registries.ITEM.getId(s.getItem());
        out.addProperty("i", iid == null ? "?" : iid.toString());
        out.addProperty("c", s.getCount());
        String b64 = Conservation.stackToB64(s);
        if (b64 == null) {
            // Encoding failed; bridge will see no `n` and reject on commit.
            return ClaudeIo.reply(ctx, out);
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
        return ClaudeIo.reply(ctx, out);
    }

    public static int readSlotPart(CommandContext<ServerCommandSource> ctx) {
        String id = StringArgumentType.getString(ctx, "txn_id");
        int slot = IntegerArgumentType.getInteger(ctx, "slot");
        int idx = IntegerArgumentType.getInteger(ctx, "chunk_idx");
        PendingTxn t = PendingTxn.TXNS.get(id);
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
            b64 = Conservation.stackToB64(s);
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
        return ClaudeIo.reply(ctx, out);
    }

    public static int txnSlotPart(CommandContext<ServerCommandSource> ctx) {
        String id = StringArgumentType.getString(ctx, "txn_id");
        int slot = IntegerArgumentType.getInteger(ctx, "slot");
        int idx = IntegerArgumentType.getInteger(ctx, "chunk_idx");
        String part = StringArgumentType.getString(ctx, "part").trim();
        PendingTxn t = PendingTxn.TXNS.get(id);
        if (t == null) return errOf(ctx, "unknown_txn", "no such txn_id (or expired)");
        if (!t.isWrite) return errOf(ctx, "wrong_txn_kind", "this txn was opened for read, not write");
        if (slot < 0 || slot >= t.totalSlots) return errOf(ctx, "bad_slot",
            "slot out of range [0, " + t.totalSlots + ")");
        Map<Integer, String> frags = t.writeFragments.computeIfAbsent(slot, k -> new HashMap<>());
        frags.put(idx, part);
        t.bumpTtl();
        JsonObject out = new JsonObject();
        out.addProperty("ok", true);
        return ClaudeIo.reply(ctx, out);
    }

    public static int txnSlotFinish(CommandContext<ServerCommandSource> ctx) {
        String id = StringArgumentType.getString(ctx, "txn_id");
        int slot = IntegerArgumentType.getInteger(ctx, "slot");
        int count = IntegerArgumentType.getInteger(ctx, "count");
        PendingTxn t = PendingTxn.TXNS.get(id);
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
            stack = Conservation.stackFromB64(sb.toString());
            stack.setCount(count);
        } catch (Exception e) {
            return errOf(ctx, "bad_stack", "stack decode failed: " + e.getMessage());
        }
        t.layout.put(slot, stack);
        t.bumpTtl();
        JsonObject out = new JsonObject();
        out.addProperty("ok", true);
        return ClaudeIo.reply(ctx, out);
    }

    public static int closeTxn(CommandContext<ServerCommandSource> ctx) {
        String id = StringArgumentType.getString(ctx, "txn_id");
        PendingTxn t = PendingTxn.TXNS.remove(id);
        JsonObject out = new JsonObject();
        out.addProperty("ok", t != null);
        return ClaudeIo.reply(ctx, out);
    }

    public static int txnSlot(CommandContext<ServerCommandSource> ctx) {
        String id = StringArgumentType.getString(ctx, "txn_id");
        int slot = IntegerArgumentType.getInteger(ctx, "slot");
        String stackArg = StringArgumentType.getString(ctx, "stack").trim();
        PendingTxn t = PendingTxn.TXNS.get(id);
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
                stack = Conservation.stackFromB64(stackArg.substring(colon + 1));
                stack.setCount(count);
            } catch (Exception e) {
                return errOf(ctx, "bad_stack", "stack decode failed: " + e.getMessage());
            }
        }
        t.layout.put(slot, stack);
        t.bumpTtl();

        JsonObject out = new JsonObject();
        out.addProperty("ok", true);
        return ClaudeIo.reply(ctx, out);
    }

    public static int txnCommit(CommandContext<ServerCommandSource> ctx) {
        String id = StringArgumentType.getString(ctx, "txn_id");
        PendingTxn t = PendingTxn.TXNS.get(id);
        if (t == null) return errOf(ctx, "unknown_txn", "no such txn_id (or expired)");
        if (!t.isWrite) return errOf(ctx, "wrong_txn_kind", "this txn was opened for read, not write");

        MinecraftServer server = ctx.getSource().getServer();
        ServerPlayerEntity callerPlayer = server.getPlayerManager().getPlayer(t.caller);
        if (callerPlayer == null) {
            PendingTxn.TXNS.remove(id);
            return errOf(ctx, "caller_offline", "caller went offline");
        }

        ServerWorld world = null;
        if (t.dim != null) {
            world = server.getWorld(RegistryKey.of(RegistryKeys.WORLD, t.dim));
            if (world == null) {
                PendingTxn.TXNS.remove(id);
                return errOf(ctx, "bad_dim", "world unloaded");
            }
        }

        // Re-resolve target on the live world (BlockEntity may have been
        // replaced; backpack may have been unequipped).
        ResolvedTarget rt;
        try {
            rt = TargetResolver.resolveTarget(server, callerPlayer, t.kind, world, t.pos);
        } catch (TargetException te) {
            PendingTxn.TXNS.remove(id);
            return errOf(ctx, te.code, te.getMessage());
        }
        if (rt.totalSlots != t.totalSlots) {
            PendingTxn.TXNS.remove(id);
            return errOf(ctx, "shape_changed", "slot count changed since open");
        }

        // Re-check viewer guard at commit time (TOCTOU — someone may have
        // opened the chest between txn_open and txn_commit).
        List<String> viewers = SafetyChecks.findInventoryViewers(server, rt.viewerInventories);
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
        String liveHash = Conservation.hashContents(live, false);
        if (!liveHash.equalsIgnoreCase(t.openContentsHash)) {
            JsonObject extra = new JsonObject();
            extra.addProperty("expected", t.openContentsHash);
            extra.addProperty("actual", liveHash);
            PendingTxn.TXNS.remove(id);
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
            String mismatch = Conservation.checkConservation(live, proposed);
            if (mismatch != null) {
                JsonObject extra = new JsonObject();
                extra.addProperty("detail", mismatch);
                PendingTxn.TXNS.remove(id);
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
        PendingTxn.TXNS.remove(id);

        JsonObject out = new JsonObject();
        out.addProperty("ok", true);
        out.addProperty("applied", t.totalSlots);
        out.addProperty("snapshot_pre_hash", t.openContentsHash);
        return ClaudeIo.reply(ctx, out);
    }

    // ----- error formatting -------------------------------------------------
    // Write-side errors carry a machine-readable code + human detail + optional
    // extra fields. Distinct from ClaudeIo.error (which only has a single
    // message string) — the bridge dispatches on `error` here.

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
        String json = ClaudeIo.GSON.toJson(o);
        if (json.length() > ClaudeIo.MAX_RESPONSE_CHARS) {
            json = json.substring(0, ClaudeIo.MAX_RESPONSE_CHARS - 1);
        }
        final String out = json;
        ctx.getSource().sendFeedback(() -> Text.literal(out), false);
        return 0;
    }
}
