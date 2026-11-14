#!/usr/bin/env python3
"""Rank every droppable gear piece 1-5 stars for the randomizer server.

Inputs
  - website/content/wiki/items.json   S2 basic features + S3 characteristics
  - SItemData.tsv                      item -> tokusei IDs (S1 set bonuses);
                                       decrypted + flattened on the fly when
                                       --sitem-tsv is not given
  - comp_hack runtime tokusei XML      structured tokusei effects, conditions
  - randomizer/stat-tiers.csv      YOUR stat ranking (edit tier)
  - randomizer/gear-overrides.csv  per-item star / exclude

Outputs (randomizer/)
  - stat-tiers.csv          rewritten: keeps your tier/lower_is_better/note,
                            refreshes the count/typical/max columns, adds new
                            stats with tier blank
  - out/gear-stars.json     star -> item IDs (input for the channel drop hook)
  - out/gear-stars.csv      review sheet: every item, star, score, top stats

Scoring
  Each stat line contributes  tier_weight * min(value / typical, CAP) * factor
  where typical is the stat's 90th percentile across gear, factor is 1 for an
  always-on bonus, CONDITIONAL_FACTOR when the tokusei has conditions, and
  PARTNER_FACTOR when it buffs the partner demon. Stars are cut per slot by
  score percentile (STAR_CUTS), so every slot has its own 1-5 star spread.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Built comp tools + runtime datastore (tokusei XML, SItemData.sbin) live in the
# creative checkout; the randomizer fork reads the same game data.
COMP = ROOT.parent / "comp_hack"
DATA_DIR = ROOT / "randomizer"

sys.path.insert(0, str(ROOT / "ops"))
from wiki_export import ASPECT_LABELS, STAT_LABELS  # noqa: E402

TIER_WEIGHTS = {1: 10.0, 2: 6.0, 3: 4.0, 4: 2.5, 5: 1.5, 6: 0.5, 0: 0.0}
CONDITIONAL_FACTOR = 0.5
PARTNER_FACTOR = 0.35
CAP = 2.0
# Cumulative percentile -> star, applied per slot (40/25/18/12/5 split).
STAR_CUTS = [(0.40, 1), (0.65, 2), (0.83, 3), (0.95, 4), (1.01, 5)]
SKIP_SLOTS = {"None", "Bullets"}
# Debug / GM / never-shipped items, matched on name + description.
EXCLUDE_RX = re.compile(
    r"\[Debug Use\]|\bdebug|^Dummy |^Unimplemented$|^GM ", re.I)

# Seed ranking. "you" = your stated priorities; "claude" = my first guess for
# you to review. Only used for stats missing from stat-tiers.csv.
SEED = {
    # tier 1
    "FINAL_CRIT_CHANCE": (1, False, "you"),
    # tier 2
    "CRITICAL": (2, False, "you"),
    "LIMIT_BREAK_MAX": (2, False, "you"),
    # tier 3
    "LB_DAMAGE": (3, False, "you"),
    "TECH_ATTACK_POWER": (3, False, "you"),
    "PURSUIT_POWER": (3, False, "you"),
    # tier 4
    "LB_CHANCE": (4, False, "you"),
    "TECH_ATTACK_RATE": (4, False, "you"),
    "PURSUIT_RATE": (4, False, "you"),
    "CHANT_TIME": (4, True, "you"),
    "COOLDOWN_TIME": (4, True, "you"),
    "RATE_CLSR": (4, False, "claude"),
    "RATE_LNGR": (4, False, "claude"),
    "RATE_SPELL": (4, False, "claude"),
    "DAMAGE_DEALT": (4, False, "claude"),
    # tier 5 — solid combat stats
    **{k: (5, False, "claude") for k in (
        "CLSR", "LNGR", "SPELL", "SUPPORT", "STR", "MAGIC", "VIT", "INT",
        "SPEED", "LUCK", "HP_MAX", "MP_MAX", "PDEF", "MDEF", "CRIT_DEF",
        "EFFECT_POWER", "SKILL_STACK_ADJUST", "STATUS_INFLICT_ADJUST",
        "CLENCH_CHANCE", "RATE_HEAL", "RATE_SUPPORT", "BOOST_HEAL",
        "BOOST_WEAPON", "BOOST_SLASH", "BOOST_THRUST", "BOOST_STRIKE",
        "BOOST_LNGR", "BOOST_PIERCE", "BOOST_SPREAD", "BOOST_FIRE",
        "BOOST_ICE", "BOOST_ELEC", "BOOST_FORCE", "BOOST_EXPEL",
        "BOOST_CURSE", "BOOST_ALMIGHTY", "BOOST_MAGICFORCE", "BOOST_NERVE",
        "BOOST_MIND",
    )},
    **{k: (5, True, "claude") for k in (
        "DAMAGE_TAKEN", "RATE_CLSR_TAKEN", "RATE_LNGR_TAKEN",
        "RATE_SPELL_TAKEN", "MP_COST_ADJUST", "HP_COST_ADJUST",
    )},
    # tier 6 — defensive / utility
    **{k: (6, False, "claude") for k in (
        "HP_REGEN", "MP_REGEN", "RATE_XP", "RATE_MACCA", "RATE_MAG",
        "RATE_EXPERTISE", "SOUL_POINT_RATE", "BETHEL_RATE",
        "DIGITALIZE_POINT_RATE", "DIGITALIZE_LENGTH", "FAMILIARITY_UP_RATE",
        "MOVE2", "SUMMON_SPEED", "CAST_INTERRUPT_NULL", "HITSTUN_NULL",
        "KNOCKBACK_NULL", "CLENCH_REFLECT_CHANCE", "RATE_HEAL_TAKEN",
    )},
    "RES_STATUS": (6, True, "claude"),
    # 0 — values are IDs or not a power stat
    **{k: (0, False, "claude") for k in (
        "SKILL_ADD", "STATUS_NULL", "STATUS_ADD", "CONSTANT_STATUS",
        "SUMMON_STATUS", "KNOCKBACK_STATUS_ADD", "TALK_RATE",
        "AFFINITY_CAP_MAX", "AFFINITY_OVERRIDE", "WEAPON_AFFINITY_OVERRIDE",
        "PURSUIT_AFFINITY_OVERRIDE", "SKILL_LOCK", "TRIGGER_EXPIRED",
        "ZONE_INSTANCE_POISON", "BOOST_SUICIDE", "FAMILIARITY_DOWN_RATE",
    )},
    "COMBAT_SPEED_NULL": (6, False, "claude"),
}
SEED_PREFIX = {"RES_": (6, False), "NRA_": (6, False)}

# CorrectTbl IDs that are already percentages whatever their Type column says.
PCT_IDS = {"LB_CHANCE", "LB_DAMAGE", "FINAL_CRIT_CHANCE", "CHANT_TIME",
           "COOLDOWN_TIME"}

LABELS = {
    "FINAL_CRIT_CHANCE": "Final crit chance (FCC)",
    "CRITICAL": "Critical",
    "LIMIT_BREAK_MAX": "Limit break cap",
    "LB_DAMAGE": "Limit break power",
    "LB_CHANCE": "Limit break chance",
    "TECH_ATTACK_POWER": "Technical attack power",
    "TECH_ATTACK_RATE": "Technical attack chance",
    "PURSUIT_POWER": "Pursuit power",
    "PURSUIT_RATE": "Pursuit chance",
    "CHANT_TIME": "Chant time (incantation)",
    "COOLDOWN_TIME": "Skill cooldown",
    "RATE_CLSR": "Close-range damage %",
    "RATE_LNGR": "Long-range (shot) damage %",
    "RATE_SPELL": "Magic damage %",
    "DAMAGE_DEALT": "Damage dealt %",
    "DAMAGE_TAKEN": "Damage taken %",
    "CLSR": "Close-range", "LNGR": "Long-range", "SPELL": "Spell",
    "SUPPORT": "Support", "MAGIC": "MAG", "SPEED": "SPD", "LUCK": "LUK",
    "PDEF": "Phys defense", "MDEF": "Magic defense",
    "MOVE2": "Move speed (MOVE2)",
    "RES_STATUS": "Resist status (status time)",
}

CSV_FIELDS = ["key", "label", "tier", "lower_is_better", "set_by", "items",
              "items_always_on", "typical", "max", "slots", "note"]


def stat_key(sid: str, ctype: int) -> str:
    if ctype == 0 or sid.startswith("RATE_") or sid in PCT_IDS:
        return sid
    return f"{sid}%"


def label_for(key: str) -> str:
    base = key.rstrip("%")
    text = (LABELS.get(base) or STAT_LABELS.get(base) or ASPECT_LABELS.get(base)
            or base.replace("_", " ").capitalize())
    return f"{text} (%)" if key.endswith("%") and "%" not in text else text


def seed_for(key: str) -> tuple[str, str, str]:
    base = key.rstrip("%")
    if base in SEED:
        tier, lower, who = SEED[base]
        return str(tier), "yes" if lower else "", who
    for prefix, (tier, lower) in SEED_PREFIX.items():
        if base.startswith(prefix):
            return str(tier), "yes" if lower else "", "claude"
    return "", "", ""


def flatten_sitem(out_dir: Path) -> Path:
    bin_dir = COMP / "build-localdeps-v31" / "bin"
    sbin = COMP / "runtime/datastore/BinaryData/Shield/SItemData.sbin"
    plain, tsv = out_dir / "SItemData.plain.bin", out_dir / "SItemData.tsv"
    subprocess.run([str(bin_dir / "comp_decrypt"), str(sbin), str(plain)],
                   check=True, capture_output=True)
    subprocess.run([str(bin_dir / "comp_bdpatch"), "flatten", "sitem",
                    str(plain), str(tsv)], check=True, capture_output=True)
    return tsv


def load_sitem(tsv: Path) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    with tsv.open(newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            iid = (row.get("ID") or "").strip()
            if iid.isdigit():
                ids = [int(x) for x in re.findall(r"-?\d+", row.get("tokusei") or "")]
                out[int(iid)] = [x for x in ids if x > 0]
    return out


def _members(obj: ET.Element) -> dict[str, ET.Element]:
    return {m.get("name"): m for m in obj.findall("member")}


def _list_objects(member: ET.Element | None) -> list[dict[str, ET.Element]]:
    if member is None:
        return []
    return [_members(o) for o in member.iter("object")
            if o.find("member") is not None and o.get("name") != "TokuseiAttributes"]


def _text(m: dict[str, ET.Element], name: str, default: str = "") -> str:
    el = m.get(name)
    return (el.text or "").strip() if el is not None else default


def load_tokusei(tokusei_dir: Path) -> dict[int, list[tuple[str, int, bool, bool]]]:
    """tokusei id -> [(stat key, value, partner, conditional)]"""
    index: dict[int, list[tuple[str, int, bool, bool]]] = {}
    for path in sorted(tokusei_dir.glob("tokusei_*.xml")):
        for obj in ET.parse(path).getroot().findall("object"):
            m = _members(obj)
            tid = int(_text(m, "ID", "0"))
            partner = _text(m, "TargetType") == "PARTNER"
            conditional = any(
                m.get(k) is not None and len(m[k])
                for k in ("Conditions", "SkillConditions", "SkillTargetConditions"))
            effects = []
            for cv in _list_objects(m.get("CorrectValues")):
                sid = _text(cv, "ID")
                if sid and not sid.isdigit():
                    effects.append((stat_key(sid, int(_text(cv, "Type", "0"))),
                                    int(_text(cv, "Value", "0")), partner, conditional))
            for cv in _list_objects(m.get("TokuseiCorrectValues")):
                sid = _text(cv, "ID")
                if sid and not sid.isdigit():
                    # Scaled by level/expertise/etc.: treat as conditional.
                    effects.append((stat_key(sid, 0), int(_text(cv, "Value", "0")),
                                    partner, True))
            for asp in _list_objects(m.get("Aspects")):
                atype = _text(asp, "Type")
                if atype:
                    effects.append((atype, int(_text(asp, "Value", "0")),
                                    partner, conditional))
            if effects:
                index[tid] = effects
    return index


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(p * (len(s) - 1) + 0.5))]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--items", type=Path,
                    default=ROOT / "website/content/wiki/items.json")
    ap.add_argument("--sitem-tsv", type=Path)
    ap.add_argument("--tokusei-dir", type=Path,
                    default=COMP / "runtime/datastore/data/tokusei")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = ap.parse_args()

    data_dir: Path = args.data_dir
    (data_dir / "out").mkdir(parents=True, exist_ok=True)
    tiers_path = data_dir / "stat-tiers.csv"
    overrides_path = data_dir / "gear-overrides.csv"
    if not overrides_path.is_file():
        overrides_path.write_text("id,star,note\n", encoding="utf-8")

    items = json.loads(args.items.read_text(encoding="utf-8"))["items"]
    if args.sitem_tsv:
        sitem = load_sitem(args.sitem_tsv)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            sitem = load_sitem(flatten_sitem(Path(tmp)))
    tokusei = load_tokusei(args.tokusei_dir)

    # item id -> [(key, value, partner, conditional)]
    gear = [i for i in items if i["equipSlot"] not in SKIP_SLOTS
            and not i["equipSlot"].startswith("Vis")]
    lines: dict[int, list[tuple[str, int, bool, bool]]] = {}
    for item in gear:
        found = []
        for stat in (item.get("basicFeatures") or []) + (item.get("characteristics") or []):
            found.append((stat_key(stat["id"], stat["type"]), stat["value"], False, False))
        for tid in sitem.get(item["id"], []):
            found.extend(tokusei.get(tid, []))
        lines[item["id"]] = found

    # Merge stat tiers: your CSV wins, seed fills new stats.
    existing = {r["key"]: r for r in read_csv(tiers_path)}
    rows: dict[str, dict[str, str]] = {}
    seen = defaultdict(lambda: {"items": set(), "always": set(), "vals": [], "slots": Counter()})
    slot_of = {i["id"]: i["equipSlot"] for i in gear}
    for iid, found in lines.items():
        for key, val, partner, cond in found:
            s = seen[key]
            s["items"].add(iid)
            s["slots"][slot_of[iid]] += 1
            if not partner and not cond:
                s["always"].add(iid)
            s["vals"].append(val)
    for key in sorted(seen, key=lambda k: -len(seen[k]["items"])):
        old = existing.get(key)
        tier, lower, who = seed_for(key)
        rows[key] = {
            "key": key,
            "label": label_for(key),
            "tier": old["tier"] if old else tier,
            "lower_is_better": old["lower_is_better"] if old else lower,
            "set_by": old["set_by"] if old else who,
            "note": (old or {}).get("note", ""),
        }
    for key, old in existing.items():  # keep stats no longer seen
        rows.setdefault(key, {k: old.get(k, "") for k in CSV_FIELDS})

    # Typical value = p90 of the good-direction magnitude.
    typical: dict[str, float] = {}
    for key, s in seen.items():
        sign = -1 if rows[key]["lower_is_better"] == "yes" else 1
        good = [sign * v for v in s["vals"] if sign * v > 0]
        typical[key] = percentile(good, 0.9) or 1.0
        rows[key].update({
            "items": str(len(s["items"])),
            "items_always_on": str(len(s["always"])),
            "typical": f"{sign * typical[key]:g}",
            "max": f"{max(s['vals'], key=lambda v: sign * v):g}",
            "slots": " ".join(k for k, _ in s["slots"].most_common(4)),
        })

    def tier_of(key: str) -> int:
        t = rows.get(key, {}).get("tier", "")
        return int(t) if t.strip().lstrip("-").isdigit() else 0

    # Score items. Per key keep the best line of each kind so expertise-tiered
    # duplicates (+5/+10/+15/+20) don't stack.
    scores: dict[int, float] = {}
    top: dict[int, list[tuple[float, str]]] = {}
    for iid, found in lines.items():
        best: dict[tuple[str, bool, bool], int] = {}
        for key, val, partner, cond in found:
            sign = -1 if rows.get(key, {}).get("lower_is_better") == "yes" else 1
            k = (key, partner, cond)
            if k not in best or sign * val > sign * best[k]:
                best[k] = val
        total, parts = 0.0, []
        for (key, partner, cond), val in best.items():
            weight = TIER_WEIGHTS.get(tier_of(key), 0.0)
            if not weight:
                continue
            sign = -1 if rows[key]["lower_is_better"] == "yes" else 1
            ratio = max(-CAP, min(CAP, sign * val / typical[key]))
            factor = (PARTNER_FACTOR if partner else 1.0) * (CONDITIONAL_FACTOR if cond else 1.0)
            pts = weight * ratio * factor
            total += pts
            tag = ("partner " if partner else "") + ("cond " if cond else "")
            parts.append((pts, f"{tag}{rows[key]['label']} {val:+d}"))
        scores[iid] = total
        top[iid] = sorted(parts, reverse=True)

    overrides = {int(r["id"]): r for r in read_csv(overrides_path)
                 if r.get("id", "").strip().isdigit()}
    excluded = {i["id"] for i in gear
                if EXCLUDE_RX.search(i["name"]) or EXCLUDE_RX.search(i.get("description") or "")}
    stars: dict[int, int] = {}
    by_slot = defaultdict(list)
    for item in gear:
        if scores[item["id"]] > 0 and item["id"] not in excluded:
            by_slot[item["equipSlot"]].append(item["id"])
    for slot, ids in by_slot.items():
        ids.sort(key=lambda i: scores[i])
        for n, iid in enumerate(ids):
            p = n / len(ids)
            stars[iid] = next(s for cut, s in STAR_CUTS if p < cut)
    for iid, row in overrides.items():
        star = int(row.get("star") or 0)
        if star:
            stars[iid] = star
        else:
            stars.pop(iid, None)

    # Write outputs.
    with tiers_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        order = sorted(rows.values(), key=lambda r: (
            tier_of(r["key"]) or 99, -int(r.get("items") or 0)))
        w.writerows(order)

    by_id = {i["id"]: i for i in gear}
    with (data_dir / "out/gear-stars.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "slot", "gender", "star", "score", "override", "top_stats"])
        for iid in sorted(by_id, key=lambda i: (by_id[i]["equipSlot"], -stars.get(i, 0), -scores[i])):
            item = by_id[iid]
            w.writerow([iid, item["name"], item["equipSlot"], item["genderLabel"],
                        stars.get(iid, 0), f"{scores[iid]:.1f}",
                        overrides.get(iid, {}).get("note", "")
                        or ("auto-excluded (debug/GM)" if iid in excluded else ""),
                        "; ".join(t for _, t in top[iid][:4])])

    pools = defaultdict(list)
    for iid, star in stars.items():
        pools[star].append(iid)
    (data_dir / "out/gear-stars.json").write_text(json.dumps({
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "starCuts": STAR_CUTS,
        "stars": {str(s): sorted(pools[s]) for s in sorted(pools)},
    }, indent=1) + "\n", encoding="utf-8")

    unranked = [k for k, r in rows.items() if not r["tier"].strip()]
    print(f"gear considered {len(gear)}, ranked {len(stars)}, "
          f"auto-excluded {len(excluded)}, overrides {len(overrides)}, "
          f"unranked {len(gear) - len(stars)}")
    print("stars:", dict(sorted(Counter(stars.values()).items())))
    if unranked:
        print(f"{len(unranked)} stats have no tier yet (scored 0):", ", ".join(unranked))


if __name__ == "__main__":
    main()
