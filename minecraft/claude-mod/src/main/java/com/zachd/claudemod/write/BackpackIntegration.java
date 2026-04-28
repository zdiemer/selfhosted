package com.zachd.claudemod.write;

import java.lang.reflect.Method;

import net.minecraft.block.entity.BlockEntity;
import net.minecraft.entity.player.PlayerEntity;
import net.minecraft.inventory.Inventory;
import net.minecraft.item.ItemStack;
import net.minecraft.server.network.ServerPlayerEntity;

import com.zachd.claudemod.ClaudeMod;

/**
 * Reflection bridge to the Travelers Backpack mod.
 *
 * The backpack jar is sideloaded via {@code modCompileOnly fileTree}, so we
 * can't hard-import its types — and its internal layout has changed between
 * the modpack's pinned version and current upstream. Reflection lets us
 * tolerate small shape drift without re-compiling against a moving target.
 */
final class BackpackIntegration {
    private BackpackIntegration() {}

    /** Returns the storage ItemStackHandler (raw object) for an equipped or world backpack. */
    static Object resolveBackpackStorage(ServerPlayerEntity caller, BlockEntity be) {
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

    static int handlerSize(Object handler) {
        try {
            return (int) handler.getClass().getMethod("getSlots").invoke(handler);
        } catch (Throwable t) { return 0; }
    }

    /** Wrap an ItemStackHandler-like object in a vanilla Inventory facade. */
    static Inventory handlerAsInventory(Object handler) {
        return new HandlerInventory(handler);
    }

    /** Trigger Travelers Backpack's component sync on an equipped wearable. */
    static void backpackSync(ServerPlayerEntity caller) {
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

    private static final class HandlerInventory implements Inventory {
        private final Object h;
        private final Method get, set, slots;

        HandlerInventory(Object h) {
            this.h = h;
            try {
                this.get = h.getClass().getMethod("getStackInSlot", int.class);
                this.slots = h.getClass().getMethod("getSlots");
                Method s = null;
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
        @Override public boolean canPlayerUse(PlayerEntity player) { return true; }
        @Override public void clear() {
            int n = size();
            for (int i = 0; i < n; i++) setStack(i, ItemStack.EMPTY);
        }
    }
}
