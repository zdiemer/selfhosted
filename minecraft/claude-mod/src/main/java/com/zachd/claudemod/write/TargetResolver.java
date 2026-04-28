package com.zachd.claudemod.write;

import java.util.ArrayList;
import java.util.List;

import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.block.BlockState;
import net.minecraft.block.ChestBlock;
import net.minecraft.block.entity.BarrelBlockEntity;
import net.minecraft.block.entity.BlockEntity;
import net.minecraft.block.entity.ChestBlockEntity;
import net.minecraft.block.entity.DispenserBlockEntity;
import net.minecraft.block.entity.DropperBlockEntity;
import net.minecraft.block.entity.HopperBlockEntity;
import net.minecraft.block.entity.ShulkerBoxBlockEntity;
import net.minecraft.block.enums.ChestType;
import net.minecraft.entity.player.PlayerInventory;
import net.minecraft.inventory.Inventory;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Direction;

/**
 * Maps a {@link Kind} + (player, world, pos) to the underlying {@link Inventory}
 * the protocol writes through, plus the metadata downstream safety checks
 * need (target block positions, viewer inventories, slot mapping).
 *
 * Inventory slot indexing for {@code Kind.INVENTORY} is intentionally narrow:
 * dense 0..26 → raw 9..35 (main inventory only). Hotbar / armor / off-hand
 * are unreachable by design.
 */
final class TargetResolver {
    private TargetResolver() {}

    // Inventory slots writable by the protocol (vanilla PlayerInventory).
    // Slots 0..8 = hotbar, 9..35 = main, 36..39 = armor, 40 = offhand.
    static final int INV_RAW_MIN = 9;
    static final int INV_RAW_MAX = 35; // inclusive
    static final int INV_DENSE_SIZE = INV_RAW_MAX - INV_RAW_MIN + 1; // 27

    static ResolvedTarget resolveTarget(MinecraftServer server,
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
                    BlockPos paired = pairedChestPos(pos, st);
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
                Object handler = BackpackIntegration.resolveBackpackStorage(caller, null);
                if (handler == null) {
                    throw new TargetException("backpack_unequipped", "caller is not wearing a backpack");
                }
                int n = BackpackIntegration.handlerSize(handler);
                int[] map = new int[n];
                for (int i = 0; i < n; i++) map[i] = i;
                Inventory inv = BackpackIntegration.handlerAsInventory(handler);
                Runnable mark = () -> BackpackIntegration.backpackSync(caller);
                return new ResolvedTarget(inv, n, null, List.of(), List.of(inv), 0, map, mark, kind);
            }
            case BACKPACK_WORLD: {
                if (!FabricLoader.getInstance().isModLoaded("travelersbackpack")) {
                    throw new TargetException("backpack_unsupported", "travelersbackpack mod not loaded");
                }
                BlockEntity be = world.getBlockEntity(pos);
                if (be == null) throw new TargetException("wrong_target_type", "no block entity at " + pos.toShortString());
                Object handler = BackpackIntegration.resolveBackpackStorage(null, be);
                if (handler == null) {
                    throw new TargetException("wrong_target_type", "block entity is not a travelersbackpack");
                }
                int n = BackpackIntegration.handlerSize(handler);
                int[] map = new int[n];
                for (int i = 0; i < n; i++) map[i] = i;
                Inventory inv = BackpackIntegration.handlerAsInventory(handler);
                Runnable mark = be::markDirty;
                return new ResolvedTarget(inv, n, "single", List.of(pos), List.of(inv), 0, map, mark, kind);
            }
            default:
                throw new TargetException("wrong_target_type", "unknown target kind");
        }
    }

    private static BlockPos pairedChestPos(BlockPos pos, BlockState st) {
        if (!(st.getBlock() instanceof ChestBlock)) return null;
        var ct = st.get(ChestBlock.CHEST_TYPE);
        if (ct == ChestType.SINGLE) return null;
        Direction facing = st.get(ChestBlock.FACING);
        Direction offset = ct == ChestType.LEFT
            ? facing.rotateYClockwise()
            : facing.rotateYCounterclockwise();
        return pos.offset(offset);
    }
}
