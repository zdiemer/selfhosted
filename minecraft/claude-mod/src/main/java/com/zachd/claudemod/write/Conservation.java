package com.zachd.claudemod.write;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.security.MessageDigest;
import java.util.Base64;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import net.minecraft.item.ItemStack;
import net.minecraft.nbt.NbtCompound;
import net.minecraft.nbt.NbtIo;
import net.minecraft.nbt.NbtTagSizeTracker;
import net.minecraft.registry.Registries;
import net.minecraft.util.Identifier;

import com.zachd.claudemod.ClaudeMod;

/**
 * Item-conservation, NBT stripping, and stack hashing/encoding for the write
 * protocol.
 *
 * Conservation = "the multiset of items present before commit equals the
 * multiset after commit". The keys are stripped of volatile NBT (durability
 * counters, RepairCost, UUIDs) so a tool that ticked between preview and
 * commit doesn't trigger a spurious mismatch — the separate full-NBT
 * contents hash still rejects any actual change at TOCTOU time.
 */
final class Conservation {
    private Conservation() {}

    // Compact NBT keys we treat as volatile when computing the conservation
    // hash. Stripping these keeps a tool that ticked durability between
    // preview and commit from causing a spurious mismatch — the contents-hash
    // (separate, full-NBT) still rejects any change at TOCTOU time.
    // Top-level keys (stack root) and tag-level keys both checked.
    private static final Set<String> VOLATILE_TOP = Set.of("Damage", "RepairCost");
    private static final Set<String> VOLATILE_TAG = Set.of(
        "Damage", "RepairCost", "UUID", "UUIDLeast", "UUIDMost"
    );

    /** Returns null on success, or a human-readable reason on mismatch. */
    static String checkConservation(List<ItemStack> a, List<ItemStack> b) {
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

    static String hashContents(List<ItemStack> stacks, boolean stripped) {
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

    static String stackToB64(ItemStack s) {
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

    static ItemStack stackFromB64(String b64) throws Exception {
        // Padded or unpadded both accepted.
        byte[] raw = Base64.getDecoder().decode(b64);
        NbtCompound nbt = NbtIo.read(new DataInputStream(new ByteArrayInputStream(raw)),
            NbtTagSizeTracker.EMPTY);
        return ItemStack.fromNbt(nbt);
    }
}
