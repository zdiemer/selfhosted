using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text.Json;
using Microsoft.Xna.Framework;
using Terraria;
using Terraria.Chat;
using Terraria.ID;
using Terraria.Localization;
using Terraria.ModLoader;

namespace ClaudeCompanion;

public class ClaudeCompanion : Mod { }

public class ClaudeCommand : ModCommand
{
    public override string Command => "claude";
    public override CommandType Type => CommandType.Server;
    public override bool IsCaseSensitive => true;
    public override string Description => "Ask Claude about this world, recipes, gear, or teleporting.";
    public override void Action(CommandCaller caller, string input, string[] args)
        => CompanionSystem.Ask(caller, string.Join(" ", args));
}

// Files are an IPC channel inside this pod, never a network service. Every
// game read/write happens on the game thread. Capabilities expire and bind to
// the original socket so a reused player slot cannot inherit a pending action.
public class CompanionSystem : ModSystem
{
    static readonly string Root = Environment.GetEnvironmentVariable("BRIDGE_DIR") ?? "/bridge";
    record Turn(CommandCaller Caller, object Socket, DateTime Expires);
    static readonly Dictionary<string, Turn> Turns = new();
    static readonly Dictionary<object, Queue<DateTime>> Rates = new();
    static readonly Dictionary<object, Vector2> Previous = new();
    static int ticks;

    public override void OnWorldLoad()
    {
        if (!Main.dedServ) return;
        Turns.Clear(); Rates.Clear(); Previous.Clear();
        foreach (string dir in new[] { "requests", "actions", "results" }) {
            Directory.CreateDirectory(Path.Combine(Root, dir));
            foreach (string file in Directory.GetFiles(Path.Combine(Root, dir))) File.Delete(file);
        }
    }

    static void Atomic(string path, object value)
    {
        File.WriteAllText(path + ".tmp", JsonSerializer.Serialize(value));
        File.Move(path + ".tmp", path, true);
    }

    public static void Ask(CommandCaller caller, string prompt)
    {
        if (!Main.dedServ || caller.Player == null) return;
        if (prompt.Length == 0 || prompt.Length > 500) {
            caller.Reply("Use /claude <question> (up to 500 characters)."); return;
        }
        var socket = Netplay.Clients[caller.Player.whoAmI].Socket;
        var now = DateTime.UtcNow;
        if (!Rates.TryGetValue(socket, out var rate)) Rates[socket] = rate = new();
        while (rate.Count > 0 && (now - rate.Peek()).TotalSeconds > 60) rate.Dequeue();
        if (rate.Count >= 5 || Turns.Values.Any(t => ReferenceEquals(t.Socket, socket))) {
            caller.Reply("Please wait for your current reply, or a moment before asking again."); return;
        }
        if (Turns.Count >= 16) { caller.Reply("Claude is busy; try again shortly."); return; }
        rate.Enqueue(now);
        string id = Guid.NewGuid().ToString("N");
        Turns[id] = new(caller, socket, now.AddMinutes(6));
        Atomic(Path.Combine(Root, "requests", id + ".json"), new {
            id, player = caller.Player.name, prompt, context = Context(caller.Player)
        });
        caller.Reply("Claude is thinking…", Color.LightSkyBlue);
    }

    static object ItemInfo(Item i) => new {
        id = i.ModItem?.FullName ?? "Terraria/" + ItemID.Search.GetName(i.type),
        name = i.Name, stack = i.stack, damage = i.damage, defense = i.defense
    };

    static object Context(Player p)
    {
        var calamity = ModLoader.TryGetMod("CalamityMod", out var mod)
            ? mod.Code.GetType("CalamityMod.DownedBossSystem") : null;
        var flags = calamity?.GetProperties(BindingFlags.Public | BindingFlags.Static)
            .Where(f => f.PropertyType == typeof(bool) && f.Name.StartsWith("downed"))
            .ToDictionary(f => f.Name, f => (bool)f.GetValue(null));
        return new {
            world = Main.worldName, hardmode = Main.hardMode, difficulty = Main.GameMode,
            daytime = Main.dayTime, time = Main.time,
            bosses = new { eye = NPC.downedBoss1, evil = NPC.downedBoss2,
                skeletron = NPC.downedBoss3, queenBee = NPC.downedQueenBee,
                destroyer = NPC.downedMechBoss1, twins = NPC.downedMechBoss2,
                skeletronPrime = NPC.downedMechBoss3, plantera = NPC.downedPlantBoss,
                golem = NPC.downedGolemBoss, cultist = NPC.downedAncientCultist,
                moonLord = NPC.downedMoonlord }, calamityBosses = flags,
            mods = ModLoader.Mods.Select(m => new { name = m.Name, version = m.Version.ToString() }),
            players = Main.player.Where(x => x.active).Select(x => new {
                name = x.name, dead = x.dead, life = x.statLife, maxLife = x.statLifeMax2,
                tileX = (int)x.Center.X / 16, tileY = (int)x.Center.Y / 16 }),
            caller = new { name = p.name, life = p.statLife, maxLife = p.statLifeMax2,
                mana = p.statMana, defense = p.statDefense,
                inventory = p.inventory.Where(i => !i.IsAir).Select(ItemInfo),
                equipment = p.armor.Where(i => !i.IsAir).Select(ItemInfo) }
        };
    }

    static object Recipes(string query)
    {
        if (query.Length < 2 || query.Length > 100) throw new Exception("Use an item name, 2–100 characters.");
        return Main.recipe.Take(Recipe.numRecipes)
            .Where(r => r.createItem.Name.Contains(query, StringComparison.OrdinalIgnoreCase)
                || (r.createItem.ModItem?.FullName ?? "").Contains(query, StringComparison.OrdinalIgnoreCase))
            .Take(20).Select(r => new {
                result = ItemInfo(r.createItem), ingredients = r.requiredItem.Where(i => !i.IsAir).Select(ItemInfo),
                stations = r.requiredTile.Where(t => t >= 0).Select(t => ModContent.GetModTile(t)?.FullName ?? TileID.Search.GetName(t) ?? t.ToString()),
                groups = r.acceptedGroups.Select(g => RecipeGroup.recipeGroups[g].GetText()),
                conditions = r.Conditions.Select(c => c.Description.Value)
            }).ToArray();
    }

    static object Act(Turn turn, string op, JsonElement args)
    {
        var p = turn.Caller.Player;
        if (op == "context") return Context(p);
        if (op == "recipes") return Recipes(args.GetProperty("query").GetString() ?? "");
        if (op == "reply") {
            string text = args.GetProperty("text").GetString() ?? "";
            // Prevent generated Terraria item/color tags and control characters.
            text = new string(text.Where(c => !char.IsControl(c) && c != '[' && c != ']').Take(1000).ToArray());
            turn.Caller.Reply("Claude: " + text, Color.LightSkyBlue);
            return "sent";
        }
        if (op == "done") return "done";
        if (p.dead) throw new Exception("Wait until you respawn to teleport.");
        if (Main.npc.Any(n => n.active && n.boss)) throw new Exception("Teleport help is paused during boss fights.");
        Vector2 destination;
        switch (op) {
            case "teleport_player":
                string target = args.GetProperty("target").GetString() ?? "";
                var other = Main.player.SingleOrDefault(x => x.active && x.name.Equals(target, StringComparison.OrdinalIgnoreCase));
                if (other == null || other.dead || other == p) throw new Exception("Choose another online, living player by exact name.");
                destination = other.position; break;
            case "teleport_home":
                // CheckSpawn validates the bed; fall back to world spawn.
                int x = p.SpawnX, y = p.SpawnY;
                if (x < 0 || y < 0 || !Player.CheckSpawn(x, y)) { x = Main.spawnTileX; y = Main.spawnTileY; }
                destination = new Vector2(x * 16 - p.width / 2, y * 16 - p.height); break;
            case "teleport_back":
                if (!Previous.TryGetValue(turn.Socket, out destination)) throw new Exception("No previous teleport this connection.");
                break;
            default: throw new Exception("Unsupported action.");
        }
        if (destination.X < 16 || destination.Y < 16 || destination.X > (Main.maxTilesX - 2) * 16
            || destination.Y > (Main.maxTilesY - 2) * 16
            || Collision.SolidCollision(destination, p.width, p.height))
            throw new Exception("The destination is blocked; try again when it is clear.");
        Previous[turn.Socket] = p.position;
        p.Teleport(destination, TeleportationStyleID.RodOfDiscord);
        p.velocity = Vector2.Zero;
        NetMessage.SendData(MessageID.TeleportEntity, -1, -1, null, 0, p.whoAmI, destination.X, destination.Y, TeleportationStyleID.RodOfDiscord);
        return "Teleported " + p.name + ".";
    }

    public override void PostUpdateEverything()
    {
        if (!Main.dedServ || ++ticks % 10 != 0) return;
        var now = DateTime.UtcNow;
        foreach (var id in Turns.Keys.ToArray()) {
            var t = Turns[id]; var p = t.Caller.Player;
            if (t.Expires < now || !p.active || !ReferenceEquals(t.Socket, Netplay.Clients[p.whoAmI].Socket))
                Turns.Remove(id);
        }
        foreach (var socket in Rates.Keys.Where(s => !Netplay.Clients.Any(c => ReferenceEquals(c.Socket, s) && c.IsConnected())).ToArray()) {
            Rates.Remove(socket); Previous.Remove(socket);
        }
        foreach (string file in Directory.GetFiles(Path.Combine(Root, "actions"), "*.json").Take(32)) {
            string result = Path.Combine(Root, "results", Path.GetFileName(file));
            try {
                using var doc = JsonDocument.Parse(File.ReadAllText(file)); var a = doc.RootElement;
                string id = a.GetProperty("turn").GetString() ?? "";
                if (!Turns.TryGetValue(id, out var turn)) throw new Exception("Request expired or player disconnected.");
                if (a.GetProperty("deadline").GetDouble() < DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0)
                    throw new Exception("Action expired.");
                string op = a.GetProperty("op").GetString() ?? "";
                var value = Act(turn, op, a.GetProperty("args"));
                Atomic(result, new { ok = true, value });
                if (op == "done") Turns.Remove(id);
            } catch (Exception e) { Atomic(result, new { ok = false, error = e.Message }); }
            finally { File.Delete(file); }
        }
        foreach (string dir in new[] { "requests", "results" })
            foreach (string file in Directory.GetFiles(Path.Combine(Root, dir)))
                if (File.GetLastWriteTimeUtc(file) < now.AddMinutes(-7)) File.Delete(file);
    }
}
