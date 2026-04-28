package com.zachd.claudemod.write;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

import net.minecraft.item.ItemStack;
import net.minecraft.util.Identifier;
import net.minecraft.util.math.BlockPos;

/**
 * Per-transaction state for a /claudemod write read or write txn.
 * Fields are package-private so the rest of the {@code write/} subsystem
 * (resolver, safety, txn handlers) can mutate them directly without
 * accessor noise.
 *
 * The static {@link #TXNS} map and {@link #sweep} entry point also live here
 * because the registry and the row type are inseparable.
 */
public final class PendingTxn {
    /** TTL for an open transaction. Bumped on each chunked write fragment. */
    static final long TXN_TTL_MS = 60_000;

    public static final Map<String, PendingTxn> TXNS = new ConcurrentHashMap<>();

    /** Periodic cleanup; called from ClaudeMod's tick handler. */
    public static void sweep() {
        long now = System.currentTimeMillis();
        TXNS.entrySet().removeIf(e -> e.getValue().expired(now));
    }

    /** Sortable txn id: ms timestamp + 8 hex chars of randomness. */
    public static String newId() {
        long ms = System.currentTimeMillis();
        String r = Long.toHexString(UUID.randomUUID().getLeastSignificantBits() & 0xFFFFFFFFL);
        while (r.length() < 8) r = "0" + r;
        return ms + "-" + r;
    }

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

    void bumpTtl() { this.expiresAt = System.currentTimeMillis() + TXN_TTL_MS; }
}
