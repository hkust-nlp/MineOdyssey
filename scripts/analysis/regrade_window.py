#!/usr/bin/env python3
"""
Re-grade a single window from a saved rollout, building the snapshot ONLY
from that window's messages (no trim residue from prior windows).

Usage:
  python3 scripts/analysis/regrade_window.py <rollout_dir> <window_index>

Env (same as batch_rollout.sh):
  MCBOTS_MODEL, MCBOTS_BASE_URL, MCBOTS_API_KEY, MCBOTS_SELF_REWARD_SCORE_MODE
"""
import json, os, re, sys, time
from pathlib import Path
from openai import OpenAI


def load_window_ranges(msgs):
    """Return list of (start_idx, end_idx, assistant_count) per window."""
    trim_idxs = [
        i for i, m in enumerate(msgs)
        if isinstance(m, dict) and m.get("mcbots_context_trim")
    ]
    bounds = [0] + trim_idxs + [len(msgs)]
    windows = []
    for i in range(len(bounds) - 1):
        s, e = bounds[i], bounds[i + 1]
        n_asst = sum(
            1 for m in msgs[s:e]
            if isinstance(m, dict)
            and m.get("role") == "assistant"
            and not m.get("mcbots_self_reward_response")
            and not m.get("mcbots_summary_response")
        )
        windows.append((s, e, n_asst))
    return windows


def sanitize(msg, drop_images=True):
    """Strip metadata flags + (optionally) image_url parts. Returns OpenAI-shaped dict."""
    role = msg.get("role")
    content = msg.get("content")
    if isinstance(content, list):
        parts = []
        for p in content:
            if not isinstance(p, dict):
                continue
            if drop_images and p.get("type") == "image_url":
                continue
            parts.append({k: v for k, v in p.items() if k in ("type", "text", "image_url")})
        out_content = parts
    else:
        out_content = content
    return {"role": role, "content": out_content}


def build_prompt(n, mode):
    if mode == "ternary":
        scale_label = "TERNARY reward (-1, 0, or +1)"
        scale_legend = (
            "Use this scale:\n"
            "  - **+1** = the response made meaningful progress\n"
            "  - **0**  = neutral\n"
            "  - **-1** = regression\n\n"
        )
        score_value_str = "-1, 0, or +1"
        score_must_str = "`score` MUST be exactly -1, 0, or +1."
    else:
        scale_label = "BINARY reward (0 or 1)"
        scale_legend = ""
        score_value_str = "0 or 1"
        score_must_str = "`score` MUST be exactly 0 or 1."
    return (
        f"[Self-Reward Request — OFFLINE REGRADE] "
        f"This context window has ended. You produced {n} assistant response(s) in this window "
        f"(each containing one <action> block). Grade each with a {scale_label}.\n\n"
        + scale_legend
        + f"**Step 1 — Define your own criteria.** What deserves each grade ({score_value_str}) here?\n\n"
        + f"**Step 2 — Apply your criteria.** Score each of the {n} responses (numbered 1..{n} in chronological order).\n\n"
        + "**Step 3 — Output one JSON object.** No prose before or after. Format:\n\n"
        + "```json\n{\n"
        + f'  "criteria": "<2-4 sentences>",\n'
        + '  "rewards": [\n'
        + f'    {{"turn": 1, "score": {score_value_str}, "reason": "<short>"}},\n'
        + "    ...\n"
        + "  ],\n"
        + f'  "overall": {{"score": {score_value_str}, "reason": "<1-2 sentences>"}}\n'
        + "}\n```\n\n"
        + f"Cover ALL {n} turns in order. {score_must_str}"
    )


def main():
    if len(sys.argv) < 3:
        print("usage: regrade_window.py <rollout_dir> <window_index>", file=sys.stderr)
        sys.exit(2)
    rollout_dir = Path(sys.argv[1])
    win_idx = int(sys.argv[2])
    msgs = json.load(open(rollout_dir / "messages.json"))
    wins = load_window_ranges(msgs)
    if win_idx < 1 or win_idx > len(wins):
        print(f"window {win_idx} out of range (1..{len(wins)})", file=sys.stderr)
        sys.exit(2)
    s, e, n_asst = wins[win_idx - 1]
    print(f"Window {win_idx}: msg [{s},{e}), {n_asst} assistant turns", file=sys.stderr)

    # Snapshot = system prompt (from msg 0/1) + ONLY this window's slice (no trim residue)
    sys_msg = next((m for m in msgs if isinstance(m, dict) and m.get("role") == "system"), None)
    snapshot = []
    if sys_msg:
        snapshot.append(sanitize(sys_msg))
    for m in msgs[s:e]:
        if not isinstance(m, dict):
            continue
        if m.get("mcbots_context_trim"):
            continue
        if m.get("mcbots_self_reward_response"):
            continue
        snapshot.append(sanitize(m))

    mode = os.environ.get("MCBOTS_SELF_REWARD_SCORE_MODE", "binary").strip() or "binary"
    snapshot.append({"role": "user", "content": [{"type": "text", "text": build_prompt(n_asst, mode)}]})

    model = os.environ.get("MCBOTS_MODEL", "kimi-k2.5")
    base_url = os.environ.get("MCBOTS_BASE_URL", "https://api.moonshot.cn/v1")
    api_key = os.environ["MCBOTS_API_KEY"]
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=300.0, max_retries=2)

    extra_body = {}
    params_json = os.environ.get("MCBOTS_MODEL_PARAMS_JSON")
    if params_json:
        extra_body = json.loads(params_json)

    print(f"Calling {model} ({n_asst} turns, {len(snapshot)} messages in snapshot)...", file=sys.stderr)
    t0 = time.time()
    resp = client.chat.completions.create(model=model, messages=snapshot, **extra_body)
    elapsed = time.time() - t0

    msg = resp.choices[0].message
    text = (getattr(msg, "content", None) or "").strip()
    if not text:
        rc = (getattr(msg, "reasoning_content", None) or "").strip()
        if rc:
            text = rc
            print(f"  recovered from reasoning_content ({len(rc)} chars)", file=sys.stderr)

    parsed = None
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            parsed = json.loads(m.group(0))
        except Exception as ex:
            print(f"  parse failed: {ex}", file=sys.stderr)

    out = {
        "window_index": win_idx,
        "trigger": "offline_regrade",
        "n_responses": n_asst,
        "raw_response": text,
        "parsed": parsed,
        "elapsed_sec": round(elapsed, 3),
    }
    out_path = rollout_dir / f"self_reward_{win_idx}_regrade.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"Saved: {out_path}  (elapsed {elapsed:.1f}s)", file=sys.stderr)
    if parsed and parsed.get("rewards"):
        scores = [r.get("score") for r in parsed["rewards"]]
        print(f"  graded={len(scores)}  mean={sum(scores)/len(scores):.3f}  1s={scores.count(1)} 0s={scores.count(0)}", file=sys.stderr)


if __name__ == "__main__":
    main()
