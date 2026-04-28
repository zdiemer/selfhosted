package com.zachd.claudemod.write;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import net.minecraft.block.BlockState;
import net.minecraft.block.DispenserBlock;
import net.minecraft.block.HopperBlock;
import net.minecraft.block.entity.BlockEntity;
import net.minecraft.block.entity.DispenserBlockEntity;
import net.minecraft.block.entity.DropperBlockEntity;
import net.minecraft.block.entity.HopperBlockEntity;
import net.minecraft.entity.Entity;
import net.minecraft.entity.vehicle.HopperMinecartEntity;
import net.minecraft.inventory.Inventory;
import net.minecraft.screen.ScreenHandler;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Box;
import net.minecraft.util.math.Direction;

/**
 * TOCTOU guards run at txn open and again at commit time:
 *  - {@link #detectAttachedHoppers} flags hoppers / droppers / dispensers
 *    that could feed or pull from the target while the bridge is mid-write,
 *    plus any nearby hopper minecart that could grab items.
 *  - {@link #findInventoryViewers} scans every online player's open
 *    {@code ScreenHandler} for slots backed by the target inventory, so we
 *    abort if anyone has the chest/inventory open in a GUI.
 */
final class SafetyChecks {
    private SafetyChecks() {}

    static JsonArray detectAttachedHoppers(ServerWorld world, List<BlockPos> targets) {
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
                        face = st.get(HopperBlock.FACING);
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
                        Direction face = st.get(DispenserBlock.FACING);
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

    static List<String> findInventoryViewers(MinecraftServer server, List<Inventory> targets) {
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
}
