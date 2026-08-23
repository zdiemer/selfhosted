# FFXIV 1.0 content inventory — Garlemald Server @ 70e54f3 (2026-08-12)

The 1.23b client's quest table (`gamedata_quests`, ported from the game's EXD data) lists **524 quests**. **75 have a server-side script** under `scripts/lua/quests/`; **449 do not**, and a quest without a script cannot be accepted. Line/hook counts are a depth signal: a fully scripted quest is typically 150+ lines with `onTalkEvent` / `onEventStarted` / `onUpdate` / `onCompleteQuest` hooks; ~20 lines of flag declarations is a stub that lets the quest marker appear and little else.

## Summary

| Group | Quests | Scripted |
|---|---|---|
| Main scenario | 21 | 12 |
| Opening / tutorial (Trl, Dft, Exc) | 18 | 6 |
| Side quests (Etc) | 172 | 44 |
| Sidequests, later patches (Wld, Spl, Noc) | 64 | 12 |
| Grand Company (Gcl/Gcg/Gcu) | 57 | 0 |
| Company / linkshell (Com) | 45 | 0 |
| Seasonal (Sum) | 9 | 0 |
| Class quests (disciples) | 96 | 1 |
| Job quests (1.21+) | 42 | 0 |

## What is not a quest, but matters as much

- **Directors / content** (`scripts/lua/directors`, `content`): 14 directors — the three city openings (Man0g/l/u 001 and 101), the opening cinematic, the after-quest warp, and the eight guildleve battle types. Guildleves are the 1.0 levelling loop and they work.
- **Battle system**: damage formulae, stat recalc, level/XP and status effects are partial in this build (upstream status). Every quest that asks you to kill something depends on this first; it is the real gap, and it is Rust, not Lua.
- **Crafting / gathering**: recipe and node tables are seeded (`042`–`046`); the synthesis and gathering minigames are not implemented.
- **Economy**: retainers, bazaar and shops have tables and scripts (`retainer.lua`, `shop.lua`, `shopgoods.lua`); markets are per-ward NPC retainers in 1.0 so they work without other players.

## How the rest gets restored

1. **Pick a quest from the table below.** Its `className` lowercased is the script path the dispatcher resolves (`Man0l0` → `quests/man/man0l0.lua`).
2. **Read the client's side.** The 1.23b client carries the quest's dialogue, cutscene triggers and event calls; `meteor-decomp` and the archived packet captures show what the retail server answered at each step.
3. **Write the five hooks** (`canAcceptQuest`, `onTalkEvent`/`onEventStarted`, `onUpdate`, `isObjectivesComplete`, `onCompleteQuest`) against the Lua API in `docs/lua-runtime.md`, using a finished sibling (`man0l0.lua`, 300+ lines) as the pattern. Stubs take an hour; a multi-phase story quest with spawns and cutscenes, a day.
4. **Test in the client** against this server, then PR upstream — content lands for everyone and survives the next image bump.

Order that keeps a playable path ahead of a solo player: finish the three city main-scenario arcs (`Man0*`, `Man1*`), then the class quests for the classes you play, then `Etc` in the order the cities hand them out. Job quests (`War`…`Drg`) and the Grand Company arcs need the battle system first.


## Main scenario — 12/21 scripted

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110001 | Shapeless Melody | Man0l0 | 1 | `quests/man/man0l0.lua` | 316 | 10 |
| 110002 | Treasures of the Main | Man0l1 | 1 | `quests/man/man0l1.lua` | 1083 | 15 |
| 110005 | Sundered Skies | Man0g0 | 1 | `quests/man/man0g0.lua` | 262 | 10 |
| 110006 | Souls Gone Wild | Man0g1 | 1 | `quests/man/man0g1.lua` | 976 | 15 |
| 110009 | Flowers for All | Man0u0 | 1 | `quests/man/man0u0.lua` | 364 | 11 |
| 110010 | Court in the Sands | Man0u1 | 1 | `quests/man/man0u1.lua` | 1156 | 21 |
| 110003 | Legends Adrift | Man1l0 | 8 | `quests/man/man1l0.lua` | 474 | 10 |
| 110007 | Whispers in the Wood | Man1g0 | 8 | `quests/man/man1g0.lua` | 334 | 7 |
| 110011 | Golden Sacrifices | Man1u0 | 8 | — | | |
| 110004 | Never the Twain Shall Meet | Man2l0 | 13 | `quests/man/man2l0.lua` | 294 | 10 |
| 110008 | Beckon of the Elementals | Man2g0 | 13 | `quests/man/man2g0.lua` | 456 | 9 |
| 110012 | Calamity Cometh | Man2u0 | 13 | — | | |
| 110013 | Fade to White | Man200 | 18 | `quests/man/man200.lua` | 397 | 12 |
| 110014 | Together We Stand | Man206 | 22 | `quests/man/man206.lua` | 377 | 11 |
| 110015 | Toll of the Warden | Man300 | 30 | — | | |
| 110016 | Forever Taken | Man304 | 34 | — | | |
| 110017 | Lord Errant | Man308 | 38 | — | | |
| 110018 | Of Men They Sing | Man402 | 42 | — | | |
| 110019 | Futures Perfect | Man406 | 46 | — | | |
| 110020 | [en] | Man502 | 52 | — | | |
| 110021 | [en] | Man504 | 54 | — | | |

## Opening / tutorial (Trl, Dft, Exc) — 6/18 scripted

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110540 | Small Talk | Dftsea | 0 | `quests/dft/DftSea.lua` | 293 | 3 |
| 110541 | Small Talk | Dftfst | 0 | `quests/dft/DftFst.lua` | 278 | 4 |
| 110542 | Small Talk | Dftroc | 0 | — | | |
| 110543 | Small Talk | Dftwil | 0 | `quests/dft/DftWil.lua` | 323 | 4 |
| 110544 | Small Talk | Dftsrt | 0 | — | | |
| 110545 | Small Talk | Dftlak | 0 | — | | |
| 110100 | Bloody Baptism | Exc200 | 20 | — | | |
| 110140 | Getting Started | Trl0l1 | 20 | `quests/trl/Trl0l1.lua` | 29 | 2 |
| 110143 | [en] | Trl0l2 | 20 | — | | |
| 110101 | Two-man Crew | Exc300 | 30 | — | | |
| 110141 | Getting Started | Trl0g1 | 30 | `quests/trl/Trl0g1.lua` | 29 | 2 |
| 110102 | Captain's Orders | Exc306 | 36 | — | | |
| 110142 | [en] | Trl0u1 | 36 | `quests/trl/Trl0u1.lua` | 29 | 2 |
| 110103 | [en] | Exc400 | 40 | — | | |
| 110104 | [en] | Exc500 | 50 | — | | |
| 110144 | Selecting a Different Path Companion | Trl0l3 | 50 | — | | |
| 110105 | [en] | Exc506 | 56 | — | | |
| 110145 | [en] | Trl0l4 | 56 | — | | |

## Side quests (Etc) — 44/172 scripted

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110600 | [en] | Etc0l1 | 0 | — | | |
| 110601 | [en] | Etc0l2 | 0 | — | | |
| 110602 | [en] | Etc0l3 | 0 | — | | |
| 110603 | [en] | Etc0l4 | 0 | — | | |
| 110604 | [en] | Etc0l5 | 0 | — | | |
| 110605 | [en] | Etc0l6 | 0 | — | | |
| 110606 | [en] | Etc0l7 | 0 | — | | |
| 110607 | [en] | Etc0l8 | 0 | — | | |
| 110608 | [en] | Etc0l9 | 0 | — | | |
| 110609 | [en] | Etc0g1 | 0 | — | | |
| 110610 | [en] | Etc0g2 | 0 | — | | |
| 110611 | [en] | Etc0g3 | 0 | — | | |
| 110612 | [en] | Etc0g4 | 0 | — | | |
| 110613 | [en] | Etc0g5 | 0 | — | | |
| 110614 | [en] | Etc0g6 | 0 | — | | |
| 110615 | [en] | Etc0g7 | 0 | — | | |
| 110616 | [en] | Etc0g8 | 0 | — | | |
| 110617 | [en] | Etc0g9 | 0 | — | | |
| 110618 | [en] | Etc0u1 | 0 | — | | |
| 110619 | [en] | Etc0u2 | 0 | — | | |
| 110620 | [en] | Etc0u3 | 0 | — | | |
| 110621 | [en] | Etc0u4 | 0 | — | | |
| 110622 | [en] | Etc0u5 | 0 | — | | |
| 110623 | [en] | Etc0u6 | 0 | — | | |
| 110624 | [en] | Etc0u7 | 0 | — | | |
| 110625 | [en] | Etc0u8 | 0 | — | | |
| 110626 | [en] | Etc0u9 | 0 | — | | |
| 110635 | [en] | Etc1l2 | 0 | — | | |
| 110637 | [en] | Etc1l4 | 0 | — | | |
| 110645 | [en] | Etc2l2 | 0 | — | | |
| 110647 | [en] | Etc2l4 | 0 | — | | |
| 110649 | [en] | Etc2l6 | 0 | — | | |
| 110650 | [en] | Etc2l7 | 0 | — | | |
| 110651 | [en] | Etc2l8 | 0 | — | | |
| 110652 | [en] | Etc2l9 | 0 | — | | |
| 110657 | [en] | Etc1g3 | 0 | — | | |
| 110661 | [en] | Etc1g7 | 0 | — | | |
| 110670 | [en] | Etc2g6 | 0 | — | | |
| 110671 | [en] | Etc2g7 | 0 | — | | |
| 110672 | [en] | Etc2g8 | 0 | — | | |
| 110673 | [en] | Etc2g9 | 0 | — | | |
| 110678 | [en] | Etc1u3 | 0 | — | | |
| 110688 | [en] | Etc2u3 | 0 | — | | |
| 110689 | [en] | Etc2u4 | 0 | — | | |
| 110691 | [en] | Etc2u6 | 0 | — | | |
| 110692 | [en] | Etc2u7 | 0 | — | | |
| 110693 | [en] | Etc2u8 | 0 | — | | |
| 110694 | [en] | Etc2u9 | 0 | — | | |
| 110696 | [en] | Etc1i0 | 0 | — | | |
| 110697 | [en] | Etc1i1 | 0 | — | | |
| 110698 | [en] | Etc1i2 | 0 | — | | |
| 110699 | [en] | Etc1i3 | 0 | — | | |
| 110700 | [en] | Etc1i4 | 0 | — | | |
| 110701 | [en] | Etc1i5 | 0 | — | | |
| 110702 | [en] | Etc1i6 | 0 | — | | |
| 110703 | [en] | Etc1i7 | 0 | — | | |
| 110704 | [en] | Etc1i8 | 0 | — | | |
| 110705 | [en] | Etc1i9 | 0 | — | | |
| 110709 | [en] | Etc2i3 | 0 | — | | |
| 110710 | [en] | Etc2i4 | 0 | — | | |
| 110711 | [en] | Etc2i5 | 0 | — | | |
| 110712 | [en] | Etc2i6 | 0 | — | | |
| 110713 | [en] | Etc2i7 | 0 | — | | |
| 110714 | [en] | Etc2i8 | 0 | — | | |
| 110715 | [en] | Etc2i9 | 0 | — | | |
| 110716 | [en] | Etc3i0 | 0 | — | | |
| 110717 | [en] | Etc3i1 | 0 | — | | |
| 110718 | [en] | Etc3i2 | 0 | — | | |
| 110719 | [en] | Etc3i3 | 0 | — | | |
| 110720 | [en] | Etc3i4 | 0 | — | | |
| 110721 | [en] | Etc3i5 | 0 | — | | |
| 110722 | [en] | Etc3i6 | 0 | — | | |
| 110723 | [en] | Etc3i7 | 0 | — | | |
| 110724 | [en] | Etc3i8 | 0 | — | | |
| 110725 | [en] | Etc3i9 | 0 | — | | |
| 110729 | [en] | Etc3u4 | 0 | — | | |
| 110730 | [en] | Etc3u5 | 0 | — | | |
| 110731 | [en] | Etc3u6 | 0 | — | | |
| 110732 | [en] | Etc3u7 | 0 | — | | |
| 110733 | [en] | Etc3u8 | 0 | — | | |
| 110738 | [en] | Etc3g4 | 0 | — | | |
| 110739 | [en] | Etc3g5 | 0 | — | | |
| 110740 | [en] | Etc3g6 | 0 | — | | |
| 110741 | [en] | Etc3g7 | 0 | — | | |
| 110742 | [en] | Etc3g8 | 0 | — | | |
| 110743 | [en] | Etc3g9 | 0 | — | | |
| 110747 | [en] | Etc3l4 | 0 | — | | |
| 110748 | [en] | Etc3l5 | 0 | — | | |
| 110749 | [en] | Etc3l6 | 0 | — | | |
| 110750 | [en] | Etc3l7 | 0 | — | | |
| 110751 | [en] | Etc3l8 | 0 | — | | |
| 110752 | [en] | Etc3l9 | 0 | — | | |
| 110815 | [en] | Etc105 | 0 | — | | |
| 110820 | [en] | Etc202 | 1 | — | | |
| 110821 | the Thousand Maws of Toto’Rak | Etc202 | 1 | — | | |
| 110822 | Dzemael Darkhold | Etc202 | 1 | — | | |
| 110823 | Aurum Vale | Etc202 | 1 | — | | |
| 110824 | Cutter's Cry | Etc202 | 1 | — | | |
| 110828 | Waste Not Want Not | Etc5g0 | 1 | `quests/etc/etc5g0.lua` | 105 | 6 |
| 110838 | The Ink Thief | Etc5l0 | 1 | `quests/etc/etc5l0.lua` | 105 | 6 |
| 110848 | Ring of Deceit | Etc5u0 | 1 | `quests/etc/etc5u0.lua` | 99 | 6 |
| 110850 | [en] | Etc5u2 | 1 | — | | |
| 110851 | [en] | Etc5u3 | 1 | — | | |
| 110653 | The Tug of the Whorl | Etc3l0 | 5 | `quests/etc/etc3l0.lua` | 182 | 6 |
| 110674 | Seeing the Seers | Etc3g0 | 5 | `quests/etc/etc3g0.lua` | 185 | 6 |
| 110695 | A Call to Arms | Etc3u0 | 5 | `quests/etc/etc3u0.lua` | 184 | 6 |
| 110634 | Bridging the Gap | Etc1l1 | 10 | `quests/etc/etc1l1.lua` | 100 | 6 |
| 110654 | Proceed with Caution | Etc1g0 | 10 | — | | |
| 110659 | The Search for Sicksa | Etc1g5 | 10 | `quests/etc/etc1g5.lua` | 105 | 7 |
| 110676 | Sleepless in Eorzea | Etc1u1 | 10 | `quests/etc/etc1u1.lua` | 105 | 7 |
| 110684 | Best Flower Ever | Etc1u9 | 10 | — | | |
| 110655 | Playing with Fire | Etc1g1 | 15 | — | | |
| 110680 | An Inconvenient Dodo | Etc1u5 | 15 | `quests/etc/etc1u5.lua` | 104 | 7 |
| 110681 | Besmitten and Besmirched | Etc1u6 | 15 | `quests/etc/etc1u6.lua` | 105 | 7 |
| 110690 | No Other Dodo Will Do | Etc2u5 | 15 | — | | |
| 110726 | Quid Pro Quo | Etc3u1 | 15 | — | | |
| 110728 | Cutthroat Prices | Etc3u3 | 15 | `quests/etc/etc3u3.lua` | 102 | 6 |
| 110735 | Scrubbing the Soul | Etc3g1 | 15 | — | | |
| 110737 | A Slippery Stone | Etc3g3 | 15 | `quests/etc/etc3g3.lua` | 102 | 6 |
| 110744 | Winds of Change | Etc3l1 | 15 | — | | |
| 110746 | What a Pirate Wants | Etc3l3 | 15 | `quests/etc/etc3l3.lua` | 102 | 6 |
| 110810 | Call of Booty | Etc303 | 15 | `quests/etc/etc303.lua` | 73 | 5 |
| 110829 | In Plain Sight | Etc5g1 | 15 | `quests/etc/etc5g1.lua` | 193 | 8 |
| 110839 | Private Eyes | Etc5l1 | 15 | `quests/etc/etc5l1.lua` | 167 | 8 |
| 110849 | The Usual Suspect | Etc5u1 | 15 | `quests/etc/etc5u1.lua` | 175 | 8 |
| 110646 | A Misty Past | Etc2l3 | 17 | `quests/etc/etc2l3.lua` | 111 | 6 |
| 110666 | Stone Deaf | Etc2g2 | 18 | `quests/etc/etc2g2.lua` | 105 | 7 |
| 110811 | Risky Business | Etc101 | 18 | — | | |
| 110812 | Forging the Spirit | Etc102 | 18 | — | | |
| 110813 | Joining the Spirit | Etc103 | 18 | — | | |
| 110814 | Waking the Spirit | Etc104 | 18 | — | | |
| 110633 | Assessing the Damage | Etc1l0 | 20 | `quests/etc/etc1l0.lua` | 105 | 7 |
| 110638 | Till Death Do Us Part | Etc1l5 | 20 | `quests/etc/etc1l5.lua` | 105 | 7 |
| 110639 | Beryl Overboard | Etc1l6 | 20 | `quests/etc/etc1l6.lua` | 105 | 7 |
| 110641 | Food for Thought | Etc1l8 | 20 | `quests/etc/etc1l8.lua` | 221 | 7 |
| 110642 | Seashells by the Seashore | Etc1l9 | 20 | — | | |
| 110644 | Moonstruck | Etc2l1 | 20 | — | | |
| 110685 | The Unheard Horizon | Etc2u0 | 20 | — | | |
| 110840 | Mysteries of the Red Moon | Etc5l2 | 20 | `quests/etc/etc5l2.lua` | 141 | 7 |
| 110841 | Prophecy Inspection | Etc5l3 | 20 | `quests/etc/etc5l3.lua` | 254 | 8 |
| 110727 | There Might Be Blood | Etc3u2 | 21 | — | | |
| 110736 | Disorganized Crime | Etc3g2 | 21 | — | | |
| 110745 | Shot Through the Heart | Etc3l2 | 21 | — | | |
| 110667 | Hunting the Hunters | Etc2g3 | 24 | — | | |
| 110643 | Fishing for Answers | Etc2l0 | 25 | `quests/etc/etc2l0.lua` | 100 | 6 |
| 110656 | A Well-Balanced Diet | Etc1g2 | 25 | `quests/etc/etc1g2.lua` | 126 | 7 |
| 110669 | Losing One's Thread | Etc2g5 | 25 | — | | |
| 110706 | Counting Sheep | Etc2i0 | 25 | `quests/etc/etc2i0.lua` | 100 | 6 |
| 110707 | A Hypocritical Oath | Etc2i1 | 25 | `quests/etc/etc2i1.lua` | 105 | 7 |
| 110687 | Ore for an Ore | Etc2u2 | 28 | `quests/etc/etc2u2.lua` | 105 | 7 |
| 110640 | Have You Seen My Son | Etc1l7 | 30 | `quests/etc/etc1l7.lua` | 134 | 7 |
| 110658 | The Penultimate Prank | Etc1g4 | 30 | `quests/etc/etc1g4.lua` | 105 | 7 |
| 110662 | Say it with Wolf Tails | Etc1g8 | 30 | `quests/etc/etc1g8.lua` | 121 | 7 |
| 110663 | Embarrassing Excerpts | Etc1g9 | 30 | `quests/etc/etc1g9.lua` | 105 | 7 |
| 110664 | A Forbidden Love | Etc2g0 | 30 | `quests/etc/etc2g0.lua` | 123 | 7 |
| 110679 | The Customer Comes First | Etc1u4 | 30 | `quests/etc/etc1u4.lua` | 127 | 7 |
| 110668 | To Deskunk A Beer | Etc2g4 | 31 | — | | |
| 110686 | Freedom Isn't Free | Etc2u1 | 32 | `quests/etc/etc2u1.lua` | 105 | 7 |
| 110682 | Clasping to Hope | Etc1u7 | 34 | — | | |
| 110660 | The Ultimate Prank | Etc1g6 | 35 | `quests/etc/etc1g6.lua` | 136 | 7 |
| 110675 | A Knock in the Night | Etc1u0 | 35 | `quests/etc/etc1u0.lua` | 105 | 7 |
| 110683 | Traumaturgy | Etc1u8 | 36 | — | | |
| 110665 | Last Respects | Etc2g1 | 40 | `quests/etc/etc2g1.lua` | 105 | 7 |
| 110636 | Revenge on the Reavers | Etc1l3 | 45 | `quests/etc/etc1l3.lua` | 109 | 7 |
| 110677 | Dressed to Be Killed | Etc1u2 | 45 | `quests/etc/etc1u2.lua` | 99 | 7 |
| 110708 | Blood Price | Etc2i2 | 45 | — | | |
| 110734 | Monster of Maw Most Massive | Etc3u9 | 45 | `quests/etc/etc3u9.lua` | 105 | 7 |
| 110818 | A Light in the Dark | Etc200 | 45 | — | | |
| 110819 | What Glitters Always Isn't Gold | Etc201 | 45 | — | | |
| 110869 | Living on a Prayer | Etc304 | 45 | — | | |
| 110648 | Carving a Name | Etc2l5 | 47 | — | | |
| 110868 | A Relic Reborn | Etc106 | 50 | — | | |

## Sidequests, later patches (Wld, Spl, Noc) — 12/64 scripted

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110757 | [en] | Wld0u5 | 0 | — | | |
| 110758 | [en] | Wld0u6 | 0 | — | | |
| 110759 | [en] | Wld0u7 | 0 | — | | |
| 110760 | [en] | Wld0u8 | 0 | — | | |
| 110761 | [en] | Wld0u9 | 0 | — | | |
| 110766 | [en] | Wld0g5 | 0 | — | | |
| 110767 | [en] | Wld0g6 | 0 | — | | |
| 110768 | [en] | Wld0g7 | 0 | — | | |
| 110769 | [en] | Wld0g8 | 0 | — | | |
| 110770 | [en] | Wld0g9 | 0 | — | | |
| 110775 | [en] | Wld0l5 | 0 | — | | |
| 110776 | [en] | Wld0l6 | 0 | — | | |
| 110777 | [en] | Wld0l7 | 0 | — | | |
| 110778 | [en] | Wld0l8 | 0 | — | | |
| 110779 | [en] | Wld0l9 | 0 | — | | |
| 110780 | [en] | Wld0i1 | 0 | — | | |
| 110781 | [en] | Wld0i2 | 0 | — | | |
| 110782 | [en] | Wld0i3 | 0 | — | | |
| 110783 | [en] | Wld0i4 | 0 | — | | |
| 110784 | [en] | Wld0i5 | 0 | — | | |
| 110785 | [en] | Wld0i6 | 0 | — | | |
| 110786 | [en] | Wld0i7 | 0 | — | | |
| 110787 | [en] | Wld0i8 | 0 | — | | |
| 110788 | [en] | Wld0i9 | 0 | — | | |
| 110791 | [en] | Spl0u3 | 0 | — | | |
| 110792 | [en] | Spl0u4 | 0 | — | | |
| 110793 | [en] | Spl0u5 | 0 | — | | |
| 110796 | [en] | Spl0g3 | 0 | — | | |
| 110797 | [en] | Spl0g4 | 0 | — | | |
| 110798 | [en] | Spl0g5 | 0 | — | | |
| 110803 | [en] | Spl0i5 | 0 | — | | |
| 110806 | [en] | Spl0l3 | 0 | — | | |
| 110807 | [en] | Spl0l4 | 0 | — | | |
| 110808 | [en] | Spl0l5 | 0 | — | | |
| 110809 | Guild Tasks | Noc000 | 0 | — | | |
| 110817 | Provisioning & Supply Missions | Noc001 | 0 | — | | |
| 110799 | The Heat Is On | Spl0i1 | 1 | — | | |
| 110800 | Impish Impositions | Spl0i2 | 1 | — | | |
| 110801 | Winter Is Not Coming | Spl0i3 | 1 | — | | |
| 110802 | Gone with the Snow | Spl0i4 | 1 | — | | |
| 110858 | Seasonal Event | Spl000 | 1 | — | | |
| 110859 | Scrambled Eggs | Spl101 | 1 | — | | |
| 110860 | Bombard Backlash | Spl102 | 1 | — | | |
| 110861 | Seasonal Event (All City-states) | Spl103 | 1 | — | | |
| 110862 | Hamlet Defense | Noc002 | 1 | — | | |
| 110863 | Class is in Session | Noc003 | 1 | — | | |
| 110771 | Trading Tongueflaps | Wld0l1 | 5 | `quests/wld/wld0l1.lua` | 94 | 5 |
| 110789 | The Dreamer's Gospel (Ul'dah) | Spl0u1 | 5 | — | | |
| 110790 | The Dreamer's Dilemma (Ul'dah) | Spl0u2 | 5 | — | | |
| 110794 | The Dreamer's Gospel (Gridania) | Spl0g1 | 5 | — | | |
| 110795 | The Dreamer's Dilemma (Gridania) | Spl0g2 | 5 | — | | |
| 110804 | The Dreamer's Gospel (Limsa Lominsa) | Spl0l1 | 5 | — | | |
| 110805 | The Dreamer's Dilemma (Limsa Lominsa) | Spl0l2 | 5 | — | | |
| 110753 | Of Archons and Muses | Wld0u1 | 10 | `quests/wld/wld0u1.lua` | 94 | 5 |
| 110762 | In the Name of Science | Wld0g1 | 10 | `quests/wld/wld0g1.lua` | 105 | 7 |
| 110763 | Hearing Confession | Wld0g2 | 10 | `quests/wld/wld0g2.lua` | 165 | 6 |
| 110772 | Letting Out Orion's Belt | Wld0l2 | 10 | `quests/wld/wld0l2.lua` | 165 | 6 |
| 110765 | Spores on the Brain | Wld0g4 | 11 | `quests/wld/wld0g4.lua` | 105 | 7 |
| 110755 | Secrets Unearthed | Wld0u3 | 17 | `quests/wld/wld0u3.lua` | 94 | 5 |
| 110764 | A Bitter Oil to Swallow | Wld0g3 | 17 | `quests/wld/wld0g3.lua` | 105 | 7 |
| 110773 | Sour Grapes | Wld0l3 | 17 | `quests/wld/wld0l3.lua` | 94 | 5 |
| 110754 | Sanguine Studies | Wld0u2 | 27 | `quests/wld/wld0u2.lua` | 105 | 7 |
| 110756 | Rustproof | Wld0u4 | 28 | `quests/wld/wld0u4.lua` | 105 | 7 |
| 110774 | Sniffing Out a Profit | Wld0l4 | 37 | `quests/wld/wld0l4.lua` | 94 | 5 |

## Grand Company (Gcl/Gcg/Gcu) — 0/57 scripted

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 111421 | [en] | Gcl501 | 0 | — | | |
| 111422 | [en] | Gcl502 | 0 | — | | |
| 111423 | [en] | Gcl601 | 0 | — | | |
| 111424 | [en] | Gcl602 | 0 | — | | |
| 111425 | [en] | Gcl603 | 0 | — | | |
| 111621 | [en] | Gcg501 | 0 | — | | |
| 111622 | [en] | Gcg502 | 0 | — | | |
| 111623 | [en] | Gcg601 | 0 | — | | |
| 111624 | [en] | Gcg602 | 0 | — | | |
| 111625 | [en] | Gcg603 | 0 | — | | |
| 111821 | [en] | Gcu501 | 0 | — | | |
| 111822 | [en] | Gcu502 | 0 | — | | |
| 111823 | [en] | Gcu601 | 0 | — | | |
| 111824 | [en] | Gcu602 | 0 | — | | |
| 111825 | [en] | Gcu603 | 0 | — | | |
| 111417 | The Cove | Gcl301 | 25 | — | | |
| 111418 | Saving the Stead Instead | Gcl302 | 25 | — | | |
| 111617 | Eternal Recurrence | Gcg301 | 25 | — | | |
| 111618 | The Pen Is Mightier Than the Spear | Gcg302 | 25 | — | | |
| 111817 | Prying Eyes | Gcu301 | 25 | — | | |
| 111818 | Different Strokes | Gcu302 | 25 | — | | |
| 111416 | It Kills with Fire (Limsa Lominsa) | Gcl101 | 30 | — | | |
| 111616 | It Kills with Fire (Gridania) | Gcg101 | 30 | — | | |
| 111816 | It Kills with Fire (Ul'dah) | Gcu101 | 30 | — | | |
| 111419 | It's a Piece of Cake to Bake a Poison Cake | Gcl303 | 40 | — | | |
| 111427 | Alive | Gcl102 | 40 | — | | |
| 111619 | Woes of the Botanist | Gcg303 | 40 | — | | |
| 111627 | Two Vans are Better than One | Gcg102 | 40 | — | | |
| 111819 | A Weaver and a Mummer | Gcu303 | 40 | — | | |
| 111827 | Like Father, Like Son | Gcu102 | 40 | — | | |
| 111420 | Kobold and the Beautiful | Gcl304 | 45 | — | | |
| 111428 | The Weakest Link | Gcl701 | 45 | — | | |
| 111429 | Deus ex Machina | Gcl103 | 45 | — | | |
| 111430 | In for Garuda Wakening (Limsa Lominsa) | Gcl104 | 45 | — | | |
| 111431 | Don't Hate the Messenger (Limsa Lominsa) | Gcl105 | 45 | — | | |
| 111432 | United We Stand (Limsa Lominsa) | Gcl106 | 45 | — | | |
| 111433 | To Kill a Raven (Limsa Lominsa) | Gcl107 | 45 | — | | |
| 111620 | Gone with the Wind | Gcg304 | 45 | — | | |
| 111628 | You Don't Have the Rite | Gcg701 | 45 | — | | |
| 111629 | Shadow of the Raven | Gcg103 | 45 | — | | |
| 111630 | In for Garuda Wakening (Gridania) | Gcg104 | 45 | — | | |
| 111631 | Don't Hate the Messenger (Gridania) | Gcg105 | 45 | — | | |
| 111632 | United We Stand (Gridania) | Gcg106 | 45 | — | | |
| 111633 | To Kill a Raven (Gridania) | Gcg107 | 45 | — | | |
| 111820 | When Alchemists Cry | Gcu304 | 45 | — | | |
| 111828 | Gore a Lizard, Hurry | Gcu701 | 45 | — | | |
| 111829 | Careless Whispers | Gcu103 | 45 | — | | |
| 111830 | In for Garuda Wakening (Ul'dah) | Gcu104 | 45 | — | | |
| 111831 | Don't Hate the Messenger (Ul'dah) | Gcu105 | 45 | — | | |
| 111832 | United We Stand (Ul'dah) | Gcu106 | 45 | — | | |
| 111833 | To Kill a Raven (Ul'dah) | Gcu107 | 45 | — | | |
| 111426 | Oil Crisis | Gcl305 | 50 | — | | |
| 111434 | Patrol, Interrupted | Gcl702 | 50 | — | | |
| 111626 | A Taste for Death | Gcg305 | 50 | — | | |
| 111634 | Cure for the Common Pox | Gcg702 | 50 | — | | |
| 111826 | Challenge Accepted | Gcu305 | 50 | — | | |
| 111834 | Mess with the Goat, Get the Horns | Gcu702 | 50 | — | | |

## Company / linkshell (Com) — 0/45 scripted

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 111408 | [en] | Com0l8 | 0 | — | | |
| 111409 | [en] | Com0l9 | 0 | — | | |
| 111412 | [en] | Com5l2 | 0 | — | | |
| 111413 | [en] | Com5l3 | 0 | — | | |
| 111414 | [en] | Com5l4 | 0 | — | | |
| 111415 | [en] | Com5l5 | 0 | — | | |
| 111608 | [en] | Com0g8 | 0 | — | | |
| 111609 | [en] | Com0g9 | 0 | — | | |
| 111612 | [en] | Com5g2 | 0 | — | | |
| 111613 | [en] | Com5g3 | 0 | — | | |
| 111614 | [en] | Com5g4 | 0 | — | | |
| 111615 | [en] | Com5g5 | 0 | — | | |
| 111808 | [en] | Com0u8 | 0 | — | | |
| 111809 | [en] | Com0u9 | 0 | — | | |
| 111812 | [en] | Com5u2 | 0 | — | | |
| 111813 | [en] | Com5u3 | 0 | — | | |
| 111814 | [en] | Com5u4 | 0 | — | | |
| 111815 | [en] | Com5u5 | 0 | — | | |
| 111401 | The Price of Integrity | Com0l1 | 22 | — | | |
| 111402 | Testing the Waters | Com0l2 | 22 | — | | |
| 111403 | Seals for the Whorl | Com0l3 | 22 | — | | |
| 111404 | Engineering Victory | Com0l4 | 22 | — | | |
| 111601 | Breaking the Seals | Com0g1 | 22 | — | | |
| 111602 | Why Did It Have to Be Snakes | Com0g2 | 22 | — | | |
| 111603 | Adder's Nest Egg | Com0g3 | 22 | — | | |
| 111604 | The Mail Must Get Through | Com0g4 | 22 | — | | |
| 111801 | Career Opportunities | Com0u1 | 22 | — | | |
| 111802 | Kindling a Flame | Com0u2 | 22 | — | | |
| 111803 | Burning a Hole in One's Pocket | Com0u3 | 22 | — | | |
| 111804 | Arms Race | Com0u4 | 22 | — | | |
| 111405 | An Officer and a Wise Man | Com0l5 | 25 | — | | |
| 111406 | Ceruleum Shock | Com0l6 | 25 | — | | |
| 111407 | Till Sea Swallows All | Com0l7 | 25 | — | | |
| 111410 | Imperial Devices (Limsa Lominsa) | Com5l0 | 25 | — | | |
| 111605 | Their Finest Hour | Com0g5 | 25 | — | | |
| 111606 | Appetite for Destruction | Com0g6 | 25 | — | | |
| 111607 | Serenity, Purity, Sanctity | Com0g7 | 25 | — | | |
| 111610 | Imperial Devices (Gridania) | Com5g0 | 25 | — | | |
| 111805 | Burning Man | Com0u5 | 25 | — | | |
| 111806 | Know Your Enemy | Com0u6 | 25 | — | | |
| 111807 | By Fire Reborn | Com0u7 | 25 | — | | |
| 111810 | Imperial Devices (Ul'dah) | Com5u0 | 25 | — | | |
| 111411 | Into the Dark (Limsa Lominsa) | Com5l1 | 45 | — | | |
| 111611 | Into the Dark (Gridania) | Com5g1 | 45 | — | | |
| 111811 | Into the Dark (Ul'dah) | Com5u1 | 45 | — | | |

## Seasonal (Sum) — 0/9 scripted

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110628 | [en] | Sum7l0 | 0 | — | | |
| 110629 | [en] | Sum7t0 | 0 | — | | |
| 110630 | [en] | Sum8a0 | 0 | — | | |
| 110631 | [en] | Sum8l0 | 0 | — | | |
| 110632 | [en] | Sum8t0 | 0 | — | | |
| 110627 | Ifrit Bleeds, We Can Kill It | Sum6a0 | 45 | — | | |
| 110816 | A Feast of Fools | Sum6m0 | 45 | — | | |
| 110867 | Taming the Tempest | Sum6g0 | 45 | — | | |
| 110870 | The Raven, Nevermore | Sum6w0 | 45 | — | | |

## Class quests (disciples) — 1/96 scripted


### Pugilist (Pgl) — 1/4

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110060 | The House Always Wins | Pgl200 | 20 | `quests/pgl/pgl200.lua` | 302 | 7 |
| 110061 | Here There Be Pirates | Pgl300 | 30 | — | | |
| 110062 | Two Sides to Every Chip | Pgl306 | 36 | — | | |
| 110063 | [en] | Pgl400 | 40 | — | | |

### Gladiator (Gla) — 0/4

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110080 | All Bark and No Bite | Gla200 | 20 | — | | |
| 110081 | Unalienable Rights | Gla300 | 30 | — | | |
| 110082 | Thrill of the Fight | Gla306 | 36 | — | | |
| 110083 | [en] | Gla400 | 40 | — | | |

### Archer (Arc) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110160 | Filling the Quiver | Arc200 | 20 | — | | |
| 110161 | The Foreboding Forest | Arc300 | 30 | — | | |
| 110162 | There Can Be Only One | Arc306 | 36 | — | | |
| 110163 | [en] | Arc400 | 40 | — | | |
| 110164 | [en] | Arc500 | 50 | — | | |
| 110165 | [en] | Arc506 | 56 | — | | |

### Lancer (Lnc) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110180 | A Wailing Welcome | Lnc200 | 20 | — | | |
| 110181 | Culture Shock | Lnc300 | 30 | — | | |
| 110182 | Necessary Evils | Lnc306 | 36 | — | | |
| 110183 | [en] | Lnc400 | 40 | — | | |
| 110184 | [en] | Lnc500 | 50 | — | | |
| 110185 | [en] | Lnc506 | 56 | — | | |

### Thaumaturge (Thm) — 0/4

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110240 | The Big Payback | Thm200 | 20 | — | | |
| 110241 | Revelry in Rivalry | Thm300 | 30 | — | | |
| 110242 | Law and the Order | Thm306 | 36 | — | | |
| 110243 | [en] | Thm400 | 40 | — | | |

### Conjurer (Cnj) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110260 | Dendrological Duties | Cnj200 | 20 | — | | |
| 110261 | Good Knight, Sweet Dreams | Cnj300 | 30 | — | | |
| 110262 | The Call of Nature | Cnj306 | 36 | — | | |
| 110263 | [en] | Cnj400 | 40 | — | | |
| 110264 | [en] | Cnj500 | 50 | — | | |
| 110265 | [en] | Cnj506 | 56 | — | | |

### Arcanist (Acn) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110280 | [en] | Acn200 | 20 | — | | |
| 110281 | [en] | Acn300 | 30 | — | | |
| 110282 | [en] | Acn306 | 36 | — | | |
| 110283 | [en] | Acn400 | 40 | — | | |
| 110284 | [en] | Acn500 | 50 | — | | |
| 110285 | [en] | Acn506 | 56 | — | | |

### Carpenter (Wdk) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110300 | The Mouths of Babes | Wdk200 | 20 | — | | |
| 110301 | Hide and Seek Shenanigans | Wdk300 | 30 | — | | |
| 110302 | Spanning the Spectrum | Wdk306 | 36 | — | | |
| 110303 | [en] | Wdk400 | 40 | — | | |
| 110304 | [en] | Wdk500 | 50 | — | | |
| 110305 | [en] | Wdk506 | 56 | — | | |

### Blacksmith (Bsm) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110320 | An Ear for Quality | Bsm200 | 20 | — | | |
| 110321 | Song of the Sirens | Bsm300 | 30 | — | | |
| 110322 | The Sound of Silence | Bsm306 | 36 | — | | |
| 110323 | [en] | Bsm400 | 40 | — | | |
| 110324 | [en] | Bsm500 | 50 | — | | |
| 110325 | [en] | Bsm506 | 56 | — | | |

### Goldsmith (Gld) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110360 | She Walks in Beauty | Gld200 | 20 | — | | |
| 110361 | F'lhaminn's Flower | Gld300 | 30 | — | | |
| 110362 | Struck Through the Heart | Gld306 | 36 | — | | |
| 110363 | [en] | Gld400 | 40 | — | | |
| 110364 | [en] | Gld500 | 50 | — | | |
| 110365 | [en] | Gld506 | 56 | — | | |

### Leatherworker (Tan) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110380 | The Silent Partners | Tan200 | 20 | — | | |
| 110381 | Design Imposters | Tan300 | 30 | — | | |
| 110382 | Head of the Class | Tan306 | 36 | — | | |
| 110383 | [en] | Tan400 | 40 | — | | |
| 110384 | [en] | Tan500 | 50 | — | | |
| 110385 | [en] | Tan506 | 56 | — | | |

### Weaver (Wvr) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110400 | Hoodwinked | Wvr200 | 20 | — | | |
| 110401 | Dance the Night Away | Wvr300 | 30 | — | | |
| 110402 | A Fruitful Murder | Wvr306 | 36 | — | | |
| 110403 | [en] | Wvr400 | 40 | — | | |
| 110404 | [en] | Wvr500 | 50 | — | | |
| 110405 | [en] | Wvr506 | 56 | — | | |

### Alchemist (Alc) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110420 | Sleep, Cousin of Death | Alc200 | 20 | — | | |
| 110421 | The Boy and the Dragon Gay | Alc300 | 30 | — | | |
| 110422 | Dream On, Dream Away | Alc306 | 36 | — | | |
| 110423 | [en] | Alc400 | 40 | — | | |
| 110424 | [en] | Alc500 | 50 | — | | |
| 110425 | [en] | Alc506 | 56 | — | | |

### Culinarian (Cul) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110440 | Showdown | Cul200 | 20 | — | | |
| 110441 | Mystery of the Gastronome Gone Home | Cul300 | 30 | — | | |
| 110442 | Something in the Soup | Cul306 | 36 | — | | |
| 110443 | [en] | Cul400 | 40 | — | | |
| 110444 | [en] | Cul500 | 50 | — | | |
| 110445 | [en] | Cul506 | 56 | — | | |

### Miner (Min) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110460 | A Piece of History | Min200 | 20 | — | | |
| 110461 | Little Saboteurs | Min300 | 30 | — | | |
| 110462 | Runaway Little Girl | Min306 | 36 | — | | |
| 110463 | [en] | Min400 | 40 | — | | |
| 110464 | [en] | Min500 | 50 | — | | |
| 110465 | [en] | Min506 | 56 | — | | |

### Botanist (Hrv) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110480 | Gridanian Roots | Hrv200 | 20 | — | | |
| 110481 | The Grass is Always Greener | Hrv300 | 30 | — | | |
| 110482 | A Moogle Bouquet | Hrv306 | 36 | — | | |
| 110483 | [en] | Hrv400 | 40 | — | | |
| 110484 | [en] | Hrv500 | 50 | — | | |
| 110485 | [en] | Hrv506 | 56 | — | | |

### Fisher (Fsh) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 110500 | To Fight a Fishback | Fsh200 | 20 | — | | |
| 110501 | The Beast of the Barrel | Fsh300 | 30 | — | | |
| 110502 | Polishing the Mast | Fsh306 | 36 | — | | |
| 110503 | [en] | Fsh400 | 40 | — | | |
| 110504 | [en] | Fsh500 | 50 | — | | |
| 110505 | [en] | Fsh506 | 56 | — | | |

## Job quests (1.21+) — 0/42 scripted


### Warrior (War) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 111201 | Pride and Duty (Will Take You from the Mountain) | War0j1 | 15 | — | | |
| 111202 | Embracing the Beast | War0j2 | 15 | — | | |
| 111203 | Curious Gorge Goes to the Bazaar | War0j3 | 15 | — | | |
| 111204 | Looking the Part | War0j4 | 15 | — | | |
| 111205 | Proof is in the Pudding | War0j5 | 15 | — | | |
| 111206 | How to Quit You | War0j6 | 15 | — | | |

### Monk (Mnk) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 111221 | Brother from Another Mother | Mnk0j1 | 15 | — | | |
| 111222 | Insulted Intelligence | Mnk0j2 | 15 | — | | |
| 111223 | The Pursuit of Power | Mnk0j3 | 15 | — | | |
| 111224 | Good Vibrations | Mnk0j4 | 15 | — | | |
| 111225 | Five Easy Pieces | Mnk0j5 | 15 | — | | |
| 111226 | Return of the King...of Ruin | Mnk0j6 | 15 | — | | |

### White Mage (Whm) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 111241 | Seeds of Initiative | Whm0j1 | 15 | — | | |
| 111242 | When Sheep Attack | Whm0j2 | 15 | — | | |
| 111243 | Lost in Rage | Whm0j3 | 15 | — | | |
| 111244 | The Wheel of Disaster | Whm0j4 | 15 | — | | |
| 111245 | In Search of Succor | Whm0j5 | 15 | — | | |
| 111246 | The Chorus of Cataclysm | Whm0j6 | 15 | — | | |

### Black Mage (Blm) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 111261 | Hearing Voices | Blm0j1 | 15 | — | | |
| 111262 | A Time to Kill | Blm0j2 | 15 | — | | |
| 111263 | International Relations | Blm0j3 | 15 | — | | |
| 111264 | The Voidgate Breathes Gloomy | Blm0j4 | 15 | — | | |
| 111265 | Gearing Up | Blm0j5 | 15 | — | | |
| 111266 | Always Bet on Black | Blm0j6 | 15 | — | | |

### Paladin (Pld) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 111281 | Paladin's Pledge | Pld0j1 | 15 | — | | |
| 111282 | Honor Lost | Pld0j2 | 15 | — | | |
| 111283 | Power Struggles | Pld0j3 | 15 | — | | |
| 111284 | Poisoned Hearts | Pld0j4 | 15 | — | | |
| 111285 | Parley on High Ground | Pld0j5 | 15 | — | | |
| 111286 | Keeping the Oath | Pld0j6 | 15 | — | | |

### Bard (Brd) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 111301 | A Song of Bards and Bowmen | Brd0j1 | 15 | — | | |
| 111302 | The Archer's Anthem | Brd0j2 | 15 | — | | |
| 111303 | Bard's-Eye View | Brd0j3 | 15 | — | | |
| 111304 | Doing It the Bard Way | Brd0j4 | 15 | — | | |
| 111305 | Pieces of the Past | Brd0j5 | 15 | — | | |
| 111306 | Requiem for the Fallen | Brd0j6 | 15 | — | | |

### Dragoon (Drg) — 0/6

| id | Quest | class | lvl | script | lines | hooks |
|---|---|---|---|---|---|---|
| 111321 | Eye of the Dragon | Drg0j1 | 15 | — | | |
| 111322 | Lance of Fury | Drg0j2 | 15 | — | | |
| 111323 | Unfading Scars | Drg0j3 | 15 | — | | |
| 111324 | Double Dragoon | Drg0j4 | 15 | — | | |
| 111325 | Fatal Seduction | Drg0j5 | 15 | — | | |
| 111326 | Into the Dragon's Maw | Drg0j6 | 15 | — | | |
