"""Pure Withings decoding and conflict-safe openGym bodyweight merge."""
from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo

TYPES = {1:"weight_kg", 5:"fat_free_kg", 6:"fat_pct", 8:"fat_kg", 76:"muscle_kg", 77:"hydration_kg", 88:"bone_kg"}

def decode_groups(groups: list, tz: str = "Europe/Berlin") -> list[dict]:
    """Normalize Withings measure groups; keep all measurements without rounding kg values."""
    result = []
    zone = ZoneInfo(tz)
    for group in groups:
        if group.get("attrib") == 1:  # ignored? no: 'attrib' relates sharing, not deletion
            pass
        try:
            stamp = int(group["date"])
        except (KeyError, ValueError, TypeError):
            continue
        item = {"group_id": str(group.get("grpid", f"timestamp-{stamp}")), "timestamp": stamp,
                "day": datetime.fromtimestamp(stamp, zone).date().isoformat()}
        for measurement in group.get("measures", []):
            key = TYPES.get(measurement.get("type"))
            if not key:
                continue
            try:
                item[key] = float(measurement["value"]) * (10 ** int(measurement["unit"]))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        if any(k in item for k in TYPES.values()):
            result.append(item)
    return sorted(result, key=lambda x: (x["timestamp"], x["group_id"]))

def latest_daily_weight(measurements: list[dict]) -> dict[str, dict]:
    by_day: dict[str, dict] = {}
    for row in sorted(measurements, key=lambda x: x.get("timestamp", 0)):
        if row.get("weight_kg") is not None and row.get("weight_kg", 0) > 0:
            by_day[row["day"]] = row
    return by_day

def merge_bodyweight(state: dict, measurements: list[dict], preferred: bool = True) -> tuple[dict, dict]:
    """Preserve entire state; replace at most the bodyweight entry of matching days.

    openGym bodyweight stores w in profile units, which can be 'kg' or 'lb'.
    """
    from copy import deepcopy
    output = deepcopy(state)
    entries = list(output.get("bodyweight") or [])
    by_day = latest_daily_weight(measurements)
    unit = str(output.get("unit") or "kg").lower()
    pounds = unit in ("lb", "lbs", "imperial")
    changed = 0
    added = 0
    skipped = 0
    for day, row in sorted(by_day.items()):
        weight = row["weight_kg"] * (2.2046226218 if pounds else 1)
        weight = round(weight, 1)
        incoming = {"d":day,"w":weight,"t":row["timestamp"] * 1000}
        same_day = [i for i, e in enumerate(entries) if isinstance(e, dict) and e.get("d") == day]
        if same_day:
            if not preferred:
                skipped += 1
                continue
            idx = same_day[-1]
            old = entries[idx]
            if old.get("w") == weight and old.get("t") == incoming["t"]:
                skipped += 1
                continue
            entries[idx] = {**old, **incoming}
            # Only one weigh-in per day – remove other duplicates if already present.
            for i in reversed(same_day[:-1]):
                entries.pop(i)
            changed += 1
        else:
            entries.append(incoming)
            added += 1
    entries.sort(key=lambda x: (str(x.get("d", "")), x.get("t", 0)) if isinstance(x, dict) else ("", 0))
    output["bodyweight"] = entries
    return output, {"added":added,"replaced":changed,"unchanged":skipped,"total_withings_days":len(by_day)}
