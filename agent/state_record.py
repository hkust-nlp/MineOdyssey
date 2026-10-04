"""State record helpers for self-reward grading.

Provides:
- SNBT (Stringified NBT) mini-parser for parsing mcrcon `data get entity/block` output
- Helpers to extract a structured snapshot from parsed NBT (vitals, xp, inventory, etc.)
- A generic recursive diff that produces per-turn deltas from two snapshots
- Inventory item-level diff (added / removed / durability_changed)
"""
from __future__ import annotations

from typing import Any, Optional


# ---------- SNBT parser ----------

def parse_snbt(text: str) -> Any:
    """Parse SNBT (Stringified NBT) text into Python objects.
    Strips any mcrcon prefix like `bot has the following entity data: ` before parsing.
    Raises ValueError on malformed input."""
    s = text.strip()
    # mcrcon prefix: anything before the first `{` or `[`
    for i, ch in enumerate(s):
        if ch in '{[':
            s = s[i:]
            break
    pos = [0]
    return _parse_value(s, pos)


def _skip_ws(s: str, pos: list) -> None:
    while pos[0] < len(s) and s[pos[0]] in ' \t\n\r':
        pos[0] += 1


def _parse_value(s: str, pos: list) -> Any:
    _skip_ws(s, pos)
    if pos[0] >= len(s):
        raise ValueError("unexpected end of input")
    c = s[pos[0]]
    if c == '{':
        return _parse_compound(s, pos)
    if c == '[':
        return _parse_list(s, pos)
    if c in '"\'':
        return _parse_string(s, pos)
    return _parse_scalar(s, pos)


def _parse_compound(s: str, pos: list) -> dict:
    pos[0] += 1  # consume '{'
    out: dict = {}
    while True:
        _skip_ws(s, pos)
        if pos[0] >= len(s):
            raise ValueError("unterminated compound")
        if s[pos[0]] == '}':
            pos[0] += 1
            return out
        # key: quoted or unquoted
        if s[pos[0]] in '"\'':
            key = _parse_string(s, pos)
        else:
            start = pos[0]
            while pos[0] < len(s) and s[pos[0]] not in ': \t\n\r,}':
                pos[0] += 1
            key = s[start:pos[0]]
        _skip_ws(s, pos)
        if pos[0] >= len(s) or s[pos[0]] != ':':
            raise ValueError(f"expected ':' after key {key!r} at pos {pos[0]}")
        pos[0] += 1
        out[key] = _parse_value(s, pos)
        _skip_ws(s, pos)
        if pos[0] < len(s) and s[pos[0]] == ',':
            pos[0] += 1


def _parse_list(s: str, pos: list) -> list:
    pos[0] += 1  # consume '['
    _skip_ws(s, pos)
    # typed array prefix: [I; ... ] or [B; ...] or [L; ...]
    if pos[0] + 1 < len(s) and s[pos[0]] in 'IBLib' and s[pos[0] + 1] == ';':
        pos[0] += 2
    out: list = []
    while True:
        _skip_ws(s, pos)
        if pos[0] >= len(s):
            raise ValueError("unterminated list")
        if s[pos[0]] == ']':
            pos[0] += 1
            return out
        out.append(_parse_value(s, pos))
        _skip_ws(s, pos)
        if pos[0] < len(s) and s[pos[0]] == ',':
            pos[0] += 1


def _parse_string(s: str, pos: list) -> str:
    quote = s[pos[0]]
    pos[0] += 1
    chars: list = []
    while pos[0] < len(s) and s[pos[0]] != quote:
        if s[pos[0]] == '\\' and pos[0] + 1 < len(s):
            chars.append(s[pos[0] + 1])
            pos[0] += 2
        else:
            chars.append(s[pos[0]])
            pos[0] += 1
    if pos[0] < len(s):
        pos[0] += 1  # consume closing quote
    return ''.join(chars)


def _parse_scalar(s: str, pos: list) -> Any:
    start = pos[0]
    while pos[0] < len(s) and s[pos[0]] not in ',}] \t\n\r':
        pos[0] += 1
    tok = s[start:pos[0]].strip()
    if not tok:
        return ''
    if tok in ('true', 'false'):
        return tok == 'true'
    if tok.lower() == 'null':
        return None
    last = tok[-1]
    body = tok[:-1] if last in 'bsBSlLfFdD' else tok
    try:
        if last in 'fFdD' or '.' in body or 'e' in body.lower():
            return float(body)
        return int(body)
    except ValueError:
        return tok  # bare word string


# ---------- Inventory parsing ----------

def parse_inventory_items(items: Any) -> tuple[dict, dict]:
    """Given a parsed NBT `Inventory` list (list of compounds with `id`, `count`,
    optional `components.minecraft:damage`), return:
        (inventory_count_dict, durability_dict)
    where inventory_count_dict = {item_id: total_count_across_slots}
    and durability_dict = {item_id: damage_value_of_first_seen_slot}.
    """
    inv: dict = {}
    dur: dict = {}
    if not isinstance(items, list):
        return inv, dur
    for item in items:
        if not isinstance(item, dict):
            continue
        iid = item.get('id') or item.get('Id')
        if not isinstance(iid, str):
            continue
        cnt = item.get('count', item.get('Count', 1))
        if isinstance(cnt, (int, float)):
            inv[iid] = inv.get(iid, 0) + int(cnt)
        # damage (durability) — modern NBT uses components.minecraft:damage
        comps = item.get('components') or {}
        dmg = comps.get('minecraft:damage')
        if dmg is None:
            tag = item.get('tag') or {}  # legacy NBT
            dmg = tag.get('Damage')
        if isinstance(dmg, (int, float)) and iid not in dur:
            dur[iid] = int(dmg)
    return inv, dur


# ---------- Snapshot field extraction ----------

def extract_vitals(nbt: dict) -> dict:
    """Pull vital-stat fields out of a parsed player NBT compound."""
    out: dict = {}
    for src, dst in [
        ('Health', 'health'),
        ('AbsorptionAmount', 'absorption'),
        ('foodLevel', 'food'),
        ('foodSaturationLevel', 'food_saturation'),
        ('foodExhaustionLevel', 'food_exhaustion'),
        ('Air', 'air'),
        ('Fire', 'fire_ticks'),
        ('HurtTime', 'hurt_time'),
        ('DeathTime', 'death_time'),
        ('FallDistance', 'fall_distance'),
    ]:
        v = nbt.get(src)
        if isinstance(v, (int, float)):
            out[dst] = v
    return out


def extract_xp(nbt: dict) -> dict:
    out: dict = {}
    if isinstance(nbt.get('XpLevel'), (int, float)):
        out['level'] = int(nbt['XpLevel'])
    if isinstance(nbt.get('XpP'), (int, float)):
        out['progress'] = round(float(nbt['XpP']), 3)
    if isinstance(nbt.get('XpTotal'), (int, float)):
        out['total'] = int(nbt['XpTotal'])
    return out


def extract_handheld(inventory_items: Any, selected_slot: Any) -> Optional[dict]:
    """Return {slot, id, damage} for the item in the given hotbar slot, or None."""
    if not isinstance(inventory_items, list) or not isinstance(selected_slot, (int, float)):
        return None
    slot = int(selected_slot)
    for it in inventory_items:
        if not isinstance(it, dict):
            continue
        if int(it.get('Slot', it.get('slot', -1))) != slot:
            continue
        out: dict = {'slot': slot, 'id': it.get('id') or it.get('Id')}
        comps = it.get('components') or {}
        dmg = comps.get('minecraft:damage')
        if dmg is None:
            tag = it.get('tag') or {}
            dmg = tag.get('Damage')
        if isinstance(dmg, (int, float)):
            out['damage'] = int(dmg)
        return out
    return None


def extract_effects(nbt: dict) -> list:
    """Return list of {id, duration, amplifier} for active potion effects.
    Handles both modern (`active_effects`) and legacy (`ActiveEffects`) keys."""
    raw = nbt.get('active_effects') or nbt.get('ActiveEffects') or []
    out: list = []
    if not isinstance(raw, list):
        return out
    for eff in raw:
        if not isinstance(eff, dict):
            continue
        e: dict = {}
        e['id'] = eff.get('id') or eff.get('Id')
        for src, dst in [('duration', 'duration'), ('Duration', 'duration'),
                         ('amplifier', 'amplifier'), ('Amplifier', 'amplifier')]:
            if src in eff and isinstance(eff[src], (int, float)):
                e[dst] = int(eff[src])
        if e.get('id'):
            out.append(e)
    return out


# ---------- Delta computation ----------

def compute_state_delta(prev: Optional[dict], cur: Optional[dict]) -> dict:
    """Compute a structured diff between two snapshots.
    - Inventory keys diffed item-wise (added/removed/durability_changed).
    - Numeric values → output the difference (cur - prev).
    - String/bool/list/None → output new value.
    - Nested dicts → only changed sub-keys included.
    Returns {"no_change": True} if nothing differs."""
    if not prev or not cur:
        return {}
    delta: dict = {}
    inv_d = _diff_inventory(prev.get('inventory') or {}, cur.get('inventory') or {})
    if inv_d:
        delta['inventory'] = inv_d
    dur_d = _diff_durability(prev.get('inventory_durability') or {}, cur.get('inventory_durability') or {})
    if dur_d:
        delta['inventory_durability'] = dur_d
    skip = {'inventory', 'inventory_durability'}
    for key in cur.keys():
        if key in skip:
            continue
        sub = _diff_value(prev.get(key), cur.get(key))
        if sub is not None and sub != {} and sub != []:
            delta[key] = sub
    # Detect keys that disappeared (present in prev but not cur)
    for key in prev.keys():
        if key in skip or key in cur:
            continue
        delta[key] = None
    return delta or {"no_change": True}


def _diff_value(p: Any, c: Any) -> Any:
    if p == c:
        return None
    if isinstance(p, bool) or isinstance(c, bool):
        return c  # bool first (avoids isinstance(True, int) confusion)
    if isinstance(p, (int, float)) and isinstance(c, (int, float)):
        d = c - p
        return round(d, 3) if isinstance(d, float) else d
    if isinstance(p, dict) and isinstance(c, dict):
        sub: dict = {}
        for k in c.keys():
            d = _diff_value(p.get(k), c.get(k))
            if d is not None:
                sub[k] = d
        for k in p.keys():
            if k not in c:
                sub[k] = None
        return sub or None
    if isinstance(p, list) and isinstance(c, list):
        return c
    return c


def _diff_inventory(p_inv: dict, c_inv: dict) -> dict:
    added: dict = {}
    removed: dict = {}
    for iid, cnt in c_inv.items():
        old = p_inv.get(iid, 0)
        if cnt > old:
            added[iid] = cnt - old
    for iid, cnt in p_inv.items():
        new = c_inv.get(iid, 0)
        if cnt > new:
            removed[iid] = cnt - new
    out: dict = {}
    if added:
        out['added'] = added
    if removed:
        out['removed'] = removed
    return out


def _diff_durability(p_dur: dict, c_dur: dict) -> dict:
    out: dict = {}
    for iid, dmg in c_dur.items():
        old = p_dur.get(iid)
        if old is None:
            out[iid] = dmg  # newly tracked tool
        elif dmg != old:
            out[iid] = dmg - old
    return out
