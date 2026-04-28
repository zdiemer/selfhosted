package com.zachd.claudemod.query;

import java.lang.reflect.Field;
import java.util.Collection;
import java.util.Map;
import java.util.Optional;

import net.minecraft.text.Text;
import net.minecraft.util.Identifier;

import com.zachd.claudemod.ClaudeMod;

/**
 * Reflection bridge to Puffish Skills' private config map.
 *
 * Why reflection: Puffish's public API ({@link net.puffish.skillsmod.api.Skill})
 * only exposes {@code getId()} — the cryptic position-hash of a skill
 * instance. Human-readable fields (title, description, sp cost, prereqs)
 * live on {@code SkillDefinitionConfig}, which is reachable only through
 * {@code SkillsMod.getInstance().categories.get()} — and {@code categories}
 * is a private field with no public accessor.
 *
 * We resolve the field once per JVM lifetime, cache it, and expose three
 * read-only operations:
 *   - {@link #getCategoryConfigs()} returns the raw {@code Map<Identifier, CategoryConfig>}
 *     (typed as {@code Object} to keep callers off the private surface)
 *   - {@link #getDefinitionIdFor} maps a skill instance id → its definition id
 *   - {@link #describeDefinition} returns title + description + cost + prereqs
 *
 * On any reflection failure (mod refactor, security manager, missing class)
 * everything returns null and the caller falls back to ID-only output —
 * that's the {@code defs_unavailable} branch in the query response.
 */
public final class PuffishConfigAccess {
    private PuffishConfigAccess() {}

    private static volatile boolean resolved = false;
    private static volatile Field categoriesField;

    public static final class DefMeta {
        public final String title;
        public final String description;
        public final int cost;
        public final int requiredSkills;
        public final int requiredSpentPoints;
        DefMeta(String t, String d, int c, int rs, int rsp) {
            this.title = t; this.description = d;
            this.cost = c; this.requiredSkills = rs; this.requiredSpentPoints = rsp;
        }
    }

    @SuppressWarnings("unchecked")
    public static Map<Identifier, Object> getCategoryConfigs() {
        try {
            ensureResolved();
            if (categoriesField == null) return null;
            Object skillsMod = net.puffish.skillsmod.SkillsMod.getInstance();
            Object listener = categoriesField.get(skillsMod);
            if (listener == null) return null;
            // ChangeListener<Optional<Map<Identifier, CategoryConfig>>> — call .get()
            Object opt = listener.getClass().getMethod("get").invoke(listener);
            if (opt instanceof Optional<?> o && o.isPresent()) {
                return (Map<Identifier, Object>) o.get();
            }
            return null;
        } catch (Throwable t) {
            ClaudeMod.LOG.warn("puffish config reflection failed: {}", t.toString());
            return null;
        }
    }

    /**
     * Lookup the definitionId backing a skill instance id within the
     * given CategoryConfig. Returns null if the id isn't present (skill
     * removed across pack reload) or if the public-method-walk fails.
     */
    public static String getDefinitionIdFor(Object categoryConfig, String skillId) {
        try {
            // CategoryConfig.getSkills() → SkillsConfig
            Object skillsConfig = categoryConfig.getClass().getMethod("getSkills").invoke(categoryConfig);
            Object opt = skillsConfig.getClass().getMethod("getById", String.class).invoke(skillsConfig, skillId);
            if (opt instanceof Optional<?> o && o.isPresent()) {
                Object skillCfg = o.get();
                return (String) skillCfg.getClass().getMethod("getDefinitionId").invoke(skillCfg);
            }
            return null;
        } catch (Throwable t) {
            return null;
        }
    }

    /**
     * Pull the human-readable metadata for a definition id from the
     * category's definition pool.
     */
    public static DefMeta describeDefinition(Object categoryConfig, String definitionId) {
        try {
            Object defs = categoryConfig.getClass().getMethod("getDefinitions").invoke(categoryConfig);
            Object opt = defs.getClass().getMethod("getById", String.class).invoke(defs, definitionId);
            if (!(opt instanceof Optional<?> o) || o.isEmpty()) return null;
            Object def = o.get();
            Class<?> dc = def.getClass();

            String title = textToString(dc.getMethod("getTitle").invoke(def));
            String desc  = textToString(dc.getMethod("getDescription").invoke(def));
            int cost     = (int) dc.getMethod("getCost").invoke(def);
            int rs       = (int) dc.getMethod("getRequiredSkills").invoke(def);
            int rsp      = (int) dc.getMethod("getRequiredSpentPoints").invoke(def);
            return new DefMeta(title, desc, cost, rs, rsp);
        } catch (Throwable t) {
            return null;
        }
    }

    private static String textToString(Object maybeText) {
        if (maybeText == null) return null;
        if (maybeText instanceof Text t) return t.getString();
        // Fallback — shouldn't be hit, but logging makes future drift easy
        // to spot.
        return maybeText.toString();
    }

    private static void ensureResolved() {
        if (resolved) return;
        synchronized (PuffishConfigAccess.class) {
            if (resolved) return;
            try {
                Class<?> cls = Class.forName("net.puffish.skillsmod.SkillsMod");
                // The field name is "categories" in 0.16.x. If it ever
                // moves we'd need to scan declared fields by type — for
                // now hard-coding is fine and the catch-all keeps callers
                // safe.
                Field f = cls.getDeclaredField("categories");
                f.setAccessible(true);
                categoriesField = f;
            } catch (Throwable t) {
                ClaudeMod.LOG.warn("puffish reflection setup failed: {}", t.toString());
                categoriesField = null;
            }
            resolved = true;
        }
    }
}
