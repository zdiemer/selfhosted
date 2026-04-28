package com.zachd.claudemod.write;

import java.util.List;

import net.minecraft.block.Block;
import net.minecraft.block.BlockState;
import net.minecraft.inventory.Inventory;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.math.BlockPos;

import com.zachd.claudemod.ClaudeMod;

/**
 * Resolved view of the target inventory + metadata used for safety checks.
 *
 * Built by {@link TargetResolver}; consumed by {@code TxnHandlers} (slot
 * reads/writes), {@link SafetyChecks} (viewer guard), and the commit path
 * (markChanged after write).
 */
final class ResolvedTarget {
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
