#!/usr/bin/env python3
"""
Re-parse self_reward_*.json files using a string-aware lenient JSON parser.

Kimi-k2.6 sometimes emits ternary scores as `+1` (invalid JSON per spec — JSON
forbids leading `+` on numbers). The agent's grader-result handler already
saves the raw text in `raw_response` even when parse fails. This script
re-parses those raw texts offline so existing rollouts can be salvaged
without re-running the experiment.

Usage:
  python3 scripts/analysis/reparse_self_reward.py <record_dir>
  python3 scripts/analysis/reparse_self_reward.py /path/to/mcbots/agent_records/rollout_enterprise_20260507_055925

Output:
  Writes self_reward_<N>_reparsed.json next to each original (does NOT
  overwrite). Prints a per-window summary.
"""
import json
import re
import sys
from pathlib import Path


def lenient_parse(blob: str):
    """Try strict json.loads first; on failure, strip leading `+` from numeric
    literals OUTSIDE strings and retry. Walks char-by-char so `+9` inside
    `"reason"` strings is preserved."""
    try:
        return json.loads(blob), False
    except Exception:
        pass
    fixed: list = []
    in_str = False
    escape = False
    i = 0
    while i < len(blob):
        c = blob[i]
        if in_str:
            fixed.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            fixed.append(c)
        elif c == "+" and i + 1 < len(blob) and blob[i + 1].isdigit():
            pass  # drop the leading +
        else:
            fixed.append(c)
        i += 1
    return json.loads("".join(fixed)), True


def reparse_file(path: Path) -> dict:
    """Returns a summary dict for this file."""
    data = json.loads(path.read_text())
    summary = {
        "file": path.name,
        "window_index": data.get("window_index"),
        "had_parsed": data.get("parsed") is not None,
        "lenient_used": False,
        "now_parsed": False,
        "n_rewards": None,
        "overall_score": None,
        "out_of_range": [],
    }
    raw = data.get("raw_response") or ""
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        summary["error"] = "no JSON object found in raw_response"
        return summary
    blob = m.group(0)
    try:
        parsed, lenient = lenient_parse(blob)
    except Exception as e:
        summary["error"] = f"parse failed even with lenient mode: {e}"
        return summary

    summary["lenient_used"] = lenient
    summary["now_parsed"] = True
    if isinstance(parsed, dict):
        rewards = parsed.get("rewards")
        if isinstance(rewards, list):
            summary["n_rewards"] = len(rewards)
            for r in rewards:
                if isinstance(r, dict) and r.get("score") not in (-1, 0, 1):
                    summary["out_of_range"].append({"turn": r.get("turn"), "score": r.get("score")})
        overall = parsed.get("overall")
        if isinstance(overall, dict):
            score = overall.get("score")
            summary["overall_score"] = score
            if score not in (-1, 0, 1):
                summary["out_of_range"].append({"turn": "overall", "score": score})

    out = dict(data)
    out["parsed"] = parsed
    out["reparsed_with_lenient"] = lenient
    out_path = path.with_name(path.stem + "_reparsed.json")
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    summary["out_path"] = out_path.name
    return summary


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    record_dir = Path(sys.argv[1])
    if not record_dir.is_dir():
        print(f"ERROR: {record_dir} is not a directory")
        sys.exit(1)
    files = sorted(record_dir.glob("self_reward_*.json"))
    files = [f for f in files if "_reparsed" not in f.name]
    if not files:
        print(f"No self_reward_*.json files in {record_dir}")
        sys.exit(0)
    print(f"Found {len(files)} file(s) in {record_dir}\n")
    total_recovered = 0
    total_already = 0
    for f in files:
        s = reparse_file(f)
        idx = s.get("window_index")
        if s.get("error"):
            print(f"  [w{idx}] ERROR: {s['error']}")
            continue
        prefix = "✓" if s["had_parsed"] else "✓ (recovered)"
        if not s["had_parsed"] and s["now_parsed"]:
            total_recovered += 1
        elif s["had_parsed"]:
            total_already += 1
        marker = " (lenient)" if s["lenient_used"] else ""
        print(
            f"  [w{idx}] {prefix}{marker}  rewards={s['n_rewards']}  "
            f"overall={s['overall_score']}  -> {s['out_path']}"
        )
        if s["out_of_range"]:
            print(f"        out-of-range: {s['out_of_range']}")
    print(f"\nDone. {total_recovered} recovered, {total_already} already parsed.")


if __name__ == "__main__":
    main()
