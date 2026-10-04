#!/usr/bin/env python3
"""
Simple HTTP server to visualize finalized or live message-history files.

Usage:
  python3 scripts/trajectory_viewer.py --port 8765
"""

import argparse
import copy
import html
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse


def find_message_files(scan_root: str, include_dirs=None):
    if include_dirs is None:
        include_dirs = ["agent_records", "eval"]
    files = []
    if not os.path.isdir(scan_root):
        return files
    for rel_dir in include_dirs:
        base = os.path.join(scan_root, rel_dir)
        if not os.path.isdir(base):
            continue
        for root, _, names in os.walk(base):
            if "messages.json" in names:
                candidate = os.path.join(root, "messages.json")
            elif "messages.jsonl" in names:
                candidate = os.path.join(root, "messages.jsonl")
            else:
                continue
            files.append(os.path.relpath(candidate, scan_root))
    files.sort()
    return files


def find_message_files_under(scan_root: str, rel_base: str):
    files = []
    base = os.path.join(scan_root, rel_base)
    if not os.path.isdir(base):
        return files
    for root, _, names in os.walk(base):
        if "messages.json" in names:
            candidate = os.path.join(root, "messages.json")
        elif "messages.jsonl" in names:
            candidate = os.path.join(root, "messages.jsonl")
        else:
            continue
        files.append(os.path.relpath(candidate, scan_root))
    files.sort()
    return files


def load_messages_html(abs_path: str):
    if abs_path.endswith(".jsonl"):
        data = []
        expected_sequence = 0
        with open(abs_path, "rb") as f:
            while True:
                raw_line = f.readline()
                if not raw_line or not raw_line.endswith(b"\n"):
                    break
                try:
                    envelope = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    break
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("schema_version") != 1
                    or envelope.get("seq") != expected_sequence
                    or isinstance(envelope.get("seq"), bool)
                    or not isinstance(envelope.get("message"), dict)
                ):
                    break
                data.append(envelope["message"])
                expected_sequence += 1
    else:
        with open(abs_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    if isinstance(data, list):
        return render_messages(data)
    return f'<pre class="text">{html.escape(json.dumps(data, ensure_ascii=False, indent=2))}</pre>'


def build_file_tree(files):
    tree = {}
    for rel_path in files:
        parts = [p for p in rel_path.split(os.sep) if p]
        node = tree
        for part in parts:
            node = node.setdefault(part, {})
    return tree


def tree_contains_selected(node, selected_parts, depth):
    if depth >= len(selected_parts):
        return True
    key = selected_parts[depth]
    child = node.get(key)
    if child is None:
        return False
    return tree_contains_selected(child, selected_parts, depth + 1)


def render_file_tree(node, selected, prefix=""):
    selected_parts = [p for p in selected.split(os.sep) if p] if selected else []
    out = ['<ul class="tree">']
    for name in sorted(node.keys()):
        child = node[name]
        rel = name if not prefix else prefix + os.sep + name
        is_leaf = len(child) == 0
        if is_leaf:
            cls = "tree-file selected" if rel == selected else "tree-file"
            out.append(
                f'<li class="tree-node leaf"><a class="{cls}" href="/view?file={html.escape(rel, quote=True)}">{html.escape(name)}</a></li>'
            )
            continue

        depth = len([p for p in rel.split(os.sep) if p])
        is_open = bool(selected_parts) and tree_contains_selected(node, selected_parts, depth - 1)
        open_attr = " open" if is_open else ""
        out.append(f'<li class="tree-node dir"><details{open_attr}><summary>{html.escape(name)}/</summary>')
        out.append(render_file_tree(child, selected, rel))
        out.append("</details></li>")
    out.append("</ul>")
    return "".join(out)


def safe_join(root: str, rel_path: str):
    # Prevent path traversal
    norm = os.path.normpath(rel_path)
    if norm.startswith("..") or os.path.isabs(norm):
        return None
    abs_path = os.path.abspath(os.path.join(root, norm))
    root_abs = os.path.abspath(root)
    if not (abs_path == root_abs or abs_path.startswith(root_abs + os.sep)):
        return None
    return abs_path


def extract_text_blob(msg):
    chunks = []
    content = msg.get("content")
    if isinstance(content, str):
        chunks.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    chunks.append(part.get("text"))
                if isinstance(part.get("reasoning_content"), str):
                    chunks.append(part.get("reasoning_content"))
                if isinstance(part.get("reasoning"), str):
                    chunks.append(part.get("reasoning"))
            elif isinstance(part, str):
                chunks.append(part)
    if isinstance(msg.get("reasoning_content"), str):
        chunks.append(msg.get("reasoning_content"))
    if isinstance(msg.get("reasoning"), str):
        chunks.append(msg.get("reasoning"))
    return "\n".join(chunks)


def split_async_chunks(text):
    lines = text.splitlines()
    chunks = []
    buf = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "Async Notice Start" in line:
            if buf:
                chunks.append(("plain", "\n".join(buf)))
                buf = []
            async_lines = [line]
            i += 1
            while i < len(lines):
                async_lines.append(lines[i])
                if "Async Notice End" in lines[i]:
                    break
                i += 1
            chunks.append(("async", "\n".join(async_lines)))
        else:
            buf.append(line)
        i += 1
    if buf:
        chunks.append(("plain", "\n".join(buf)))
    if not chunks:
        chunks.append(("plain", text))
    return chunks


def render_assistant_peer(idx, msg):
    return (
        '<div class="async-peer">'
        f'<div class="meta">#{idx} <span class="role">assistant</span></div>'
        f'<div class="msg-body">{render_message_body(msg, None)}</div>'
        '</div>'
    )


def render_message_body(msg):
    out = []
    reasoning = msg.get("reasoning_content")
    if not reasoning:
        reasoning = msg.get("reasoning")
    if reasoning:
        out.append('<div class="reasoning-label">reasoning_content</div>')
        out.append(f'<pre class="reasoning">{html.escape(str(reasoning))}</pre>')
    tool_call_id = msg.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id:
        out.append(
            '<div class="tool-call-link">tool_call_id: '
            f'<code>{html.escape(tool_call_id)}</code></div>'
        )
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        out.append('<div class="tool-calls-label">tool_calls</div>')
        for call_index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                out.append(
                    '<pre class="tool-call">'
                    f'{html.escape(json.dumps(tool_call, ensure_ascii=False, indent=2))}'
                    '</pre>'
                )
                continue
            function = tool_call.get("function")
            function = function if isinstance(function, dict) else {}
            arguments = function.get("arguments", "")
            if isinstance(arguments, str):
                try:
                    arguments = json.dumps(
                        json.loads(arguments), ensure_ascii=False, indent=2
                    )
                except json.JSONDecodeError:
                    pass
            else:
                arguments = json.dumps(arguments, ensure_ascii=False, indent=2)
            out.append('<div class="tool-call">')
            out.append(
                '<div class="tool-call-meta">'
                f'#{call_index} id=<code>{html.escape(str(tool_call.get("id", "")))}</code> '
                f'type=<code>{html.escape(str(tool_call.get("type", "")))}</code> '
                f'function=<code>{html.escape(str(function.get("name", "")))}</code>'
                '</div>'
            )
            out.append(
                f'<pre class="tool-call-arguments">{html.escape(str(arguments))}</pre>'
            )
            out.append('</div>')
    content = msg.get("content")
    if isinstance(content, str):
        out.append(f'<pre class="text">{html.escape(content)}</pre>')
    elif isinstance(content, list):
        for part_idx, part in enumerate(content):
            out.append(f'<div class="part"><div class="part-meta">part #{part_idx}</div>')
            if not isinstance(part, dict):
                out.append(f'<pre class="text">{html.escape(str(part))}</pre>')
                out.append('</div>')
                continue
            ptype = part.get("type")
            reasoning_part = part.get("reasoning_content")
            if not reasoning_part:
                reasoning_part = part.get("reasoning")
            if reasoning_part:
                out.append('<div class="reasoning-label">reasoning_content</div>')
                out.append(f'<pre class="reasoning">{html.escape(str(reasoning_part))}</pre>')
            if ptype:
                out.append(f'<div class="part-type">type: {html.escape(str(ptype))}</div>')
            if ptype == "text":
                out.append(f'<pre class="text">{html.escape(str(part.get("text", "")))}</pre>')
            elif ptype == "image_url":
                url = ""
                image_url = part.get("image_url")
                if isinstance(image_url, dict):
                    url = image_url.get("url", "")
                elif isinstance(image_url, str):
                    url = image_url
                if url:
                    safe_url = html.escape(url, quote=True)
                    out.append(f'<img class="shot" src="{safe_url}" loading="lazy" />')
                else:
                    out.append('<div class="warn">[image_url missing]</div>')
            else:
                out.append(f'<pre class="text">{html.escape(json.dumps(part, ensure_ascii=False))}</pre>')
            out.append('</div>')
    else:
        out.append(f'<pre class="text">{html.escape(str(content))}</pre>')
    return "".join(out)


def render_async_windows_block(windows):
    out = []
    for i, win_msg in enumerate(windows):
        out.append('<details class="async-group" open>')
        out.append(f'<summary>Async Notice Window #{i + 1}</summary>')
        out.append(f'<div class="async-body">{render_message_body(win_msg)}</div>')
        out.append('</details>')
    return "".join(out)


def render_message_block(idx, msg, collapsed=False, async_windows=None, turn_label=""):
    role_raw = str(msg.get("role", ""))
    role = html.escape(role_raw)
    role_class = "role-" + "".join(ch if ch.isalnum() else "-" for ch in role_raw.lower())
    open_attr = "" if collapsed else " open"
    if async_windows:
        body_html = (
            '<div class="async-grid">'
            '<div class="async-col"><div class="async-col-title">Assistant</div>'
            f'{render_message_body(msg)}'
            '</div>'
            '<div class="async-col"><div class="async-col-title">Async Notice Stream</div>'
            f'{render_async_windows_block(async_windows)}'
            '</div>'
            '</div>'
        )
    else:
        body_html = render_message_body(msg)
    label_html = f' <span class="turn-label">{turn_label}</span>' if turn_label else ''
    return (
        f'<details class="msg {role_class}"{open_attr}>'
        f'<summary class="meta">#{idx} <span class="role">{role}</span>{label_html}</summary>'
        f'<div class="msg-body">{body_html}</div>'
        "</details>"
    )


def compute_turn_labels(messages):
    """Walk the conversation and tag every *task* assistant message with (window, intra, global).
    Window boundaries are any marker that ends a grading window — kept in sync with
    agent.Agent._run_out_of_band_self_reward triggers:
      - mcbots_context_trim         (round-limit trim → trim_round trigger)
      - mcbots_summary_reset        (auto-summarize  → summary_reset trigger)
      - context_reset_first_message (LLM-failure    → hard_reset trigger)
    Indices are 1-based to match self_reward_{N}.json: window N == grading window_index N,
    intra t == rewards[].turn. Global g is 1-based (viewer-only, no grading equivalent).
    Self-reward responses and summary responses are skipped (they're meta-turns)."""
    labels = {}
    window = 1
    intra = 1
    global_idx = 1
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        if (
            m.get("mcbots_context_trim")
            or m.get("mcbots_summary_reset")
            or m.get("context_reset_first_message")
        ):
            window += 1
            intra = 1
            continue
        if m.get("role") != "assistant":
            continue
        if m.get("mcbots_self_reward_response") or m.get("mcbots_summary_response"):
            continue
        labels[i] = (window, intra, global_idx)
        intra += 1
        global_idx += 1
    return labels


def render_async_group(start_idx, end_idx, group):
    assistants = []
    others = []
    for idx, msg in group:
        if str(msg.get("role", "")).lower() == "assistant":
            assistants.append((idx, msg))
        else:
            others.append((idx, msg))
    out = [
        '<details class="async-group" open>',
        f'<summary>Async Group #{start_idx}-#{end_idx}</summary>',
        '<div class="async-grid">',
        '<div class="async-col"><div class="async-col-title">Assistant</div>',
    ]
    if assistants:
        for idx, msg in assistants:
            out.append(render_message_block(idx, msg, collapsed=False))
    else:
        out.append('<div class="empty">No assistant messages in this async group.</div>')
    out.append('</div><div class="async-col"><div class="async-col-title">Async/Other</div>')
    if others:
        for idx, msg in others:
            collapsed = str(msg.get("role", "")).lower() == "system"
            out.append(render_message_block(idx, msg, collapsed=collapsed))
    else:
        out.append('<div class="empty">No async/other messages in this async group.</div>')
    out.append('</div></div></details>')
    return "".join(out)


def _part_blob(part):
    if isinstance(part, dict):
        if isinstance(part.get("text"), str):
            return part.get("text")
        if isinstance(part.get("reasoning_content"), str):
            return part.get("reasoning_content")
        if isinstance(part.get("reasoning"), str):
            return part.get("reasoning")
        return ""
    if isinstance(part, str):
        return part
    return str(part)


def _extract_windows_from_parts(parts):
    windows = []
    kept = []
    in_window = False
    current = []
    for part in parts:
        blob = _part_blob(part)
        has_start = "Async Notice Start" in blob
        has_end = "Async Notice End" in blob
        if not in_window and has_start:
            in_window = True
            current = [part]
            if has_end:
                windows.append(current)
                current = []
                in_window = False
            continue
        if in_window:
            current.append(part)
            if has_end:
                windows.append(current)
                current = []
                in_window = False
            continue
        kept.append(part)
    if in_window and current:
        windows.append(current)
    return windows, kept


def extract_async_windows_from_message(msg):
    windows = []
    content = msg.get("content")
    if isinstance(content, str):
        chunks = split_async_chunks(content)
        plain = "\n".join([c for t, c in chunks if t == "plain"])
        windows = [{"role": "user", "content": [{"type": "text", "text": c}]} for t, c in chunks if t == "async"]
        new_msg = copy.deepcopy(msg)
        new_msg["content"] = plain
        return windows, new_msg if plain.strip() else None

    if isinstance(content, list):
        src_parts = [copy.deepcopy(p) for p in content]
        window_parts, kept_parts = _extract_windows_from_parts(src_parts)
        windows = [{"role": "user", "content": wp} for wp in window_parts]
        new_msg = copy.deepcopy(msg)
        new_msg["content"] = kept_parts
        has_visible = any(
            (
                isinstance(p, dict) and p.get("type") != "text"
            )
            or (isinstance(p, dict) and str(p.get("text", "")).strip())
            or (not isinstance(p, dict))
            for p in kept_parts
        )
        return windows, new_msg if has_visible else None
    return windows, msg


def _collect_user_parts(msgs, start):
    """Gather content parts from consecutive user messages starting at `start`.

    Returns (parts, next_index) where `parts` is a flat list of content parts
    produced by concatenating each user message's content (strings are wrapped
    as text parts), and `next_index` is the first non-user message index.
    """
    parts = []
    i = start
    while i < len(msgs) and str(msgs[i].get("role", "")).lower() == "user":
        content = msgs[i].get("content")
        if isinstance(content, list):
            parts.extend(copy.deepcopy(content))
        elif isinstance(content, str):
            parts.append({"type": "text", "text": content})
        elif content is not None:
            parts.append({"type": "text", "text": str(content)})
        i += 1
    return parts, i


def _format_idx_range(a, b):
    return str(a) if a == b else f"{a}-{b}"


def render_messages(messages):
    out = []
    msgs = [copy.deepcopy(m) for m in messages]
    turn_labels = compute_turn_labels(messages)
    i = 0
    while i < len(msgs):
        msg = msgs[i]
        role = str(msg.get("role", "")).lower()

        def _label_for(idx):
            tl = turn_labels.get(idx)
            if not tl:
                return ""
            return f"[w={tl[0]} t={tl[1]} g={tl[2]}]"

        if role == "assistant":
            user_start = i + 1
            parts, next_i = _collect_user_parts(msgs, user_start)
            if parts:
                merged = {"role": "user", "content": parts}
                windows, residual = extract_async_windows_from_message(merged)
                out.append(render_message_block(i, msg, collapsed=False, async_windows=windows, turn_label=_label_for(i)))
                if residual is not None:
                    label = _format_idx_range(user_start, next_i - 1)
                    out.append(render_message_block(label, residual, collapsed=False))
                i = next_i
            else:
                out.append(render_message_block(i, msg, collapsed=False, turn_label=_label_for(i)))
                i += 1
        elif role == "user":
            parts, next_i = _collect_user_parts(msgs, i)
            label = _format_idx_range(i, next_i - 1)
            merged = {"role": "user", "content": parts}
            out.append(render_message_block(label, merged, collapsed=False))
            i = next_i
        else:
            collapsed = role == "system"
            out.append(render_message_block(i, msg, collapsed=collapsed))
            i += 1
    return "\n".join(out)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/refresh":
            selected = qs.get("file", [""])[0]
            self.server.refresh_index()
            location = "/view"
            if selected:
                location += "?file=" + quote(selected)
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()
            return
        if parsed.path == "/refresh_one":
            selected = qs.get("file", [""])[0]
            if selected:
                self.server.refresh_one_rollout(selected)
            location = "/view"
            if selected:
                location += "?file=" + quote(selected)
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()
            return
        if parsed.path not in ("/", "/view"):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return

        files = self.server.get_files()
        selected = qs.get("file", [""])[0]
        tree = self.server.file_tree
        body_messages = ""
        error = ""
        if selected:
            if selected not in self.server.files_set:
                error = f"File not indexed: {selected}"
            else:
                try:
                    body_messages = self.server.get_rendered_file(selected)
                except Exception as e:
                    error = f"Failed to load {selected}: {e}"

        html_doc = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Trajectory Viewer</title>
  <style>
    body {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; margin: 20px; background: #f7f7f5; color: #111; }}
    .layout {{ display: flex; gap: 16px; min-height: calc(100vh - 40px); }}
    .sidebar {{ width: 420px; max-width: 45vw; overflow: auto; position: sticky; top: 12px; align-self: flex-start; max-height: calc(100vh - 40px); background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 10px; }}
    .main {{ flex: 1; min-width: 0; }}
    .toolbar {{ position: sticky; top: 0; background: #f7f7f5; padding: 12px 0; border-bottom: 1px solid #ddd; }}
    .toolbar-top {{ display: flex; align-items: center; gap: 8px; }}
    .title {{ margin: 0 0 8px 0; font-size: 14px; font-weight: 700; }}
    .btn {{ display: inline-block; text-decoration: none; color: #1b3a67; background: #e8f1ff; border: 1px solid #bfd6ff; border-radius: 6px; padding: 3px 8px; font-size: 12px; }}
    .btn:hover {{ background: #dceaff; }}
    .tree {{ list-style: none; padding-left: 14px; margin: 0; }}
    .tree-node {{ margin: 2px 0; }}
    .tree-node > details > summary {{ cursor: pointer; color: #222; user-select: none; }}
    .tree-file {{ display: inline-block; text-decoration: none; color: #333; padding: 2px 6px; border-radius: 6px; }}
    .tree-file:hover {{ background: #f3f6fb; }}
    .tree-file.selected {{ background: #e8f1ff; color: #0d3a75; font-weight: 700; }}
    .empty {{ color: #666; font-size: 12px; }}
    .msg {{ border: 1px solid #ddd; border-radius: 8px; padding: 10px; margin: 12px 0; background: #fff; }}
    details.msg {{ padding: 0; overflow: hidden; }}
    details.msg > summary.meta {{ list-style: none; cursor: pointer; padding: 10px; margin: 0; border-bottom: 1px solid transparent; }}
    details.msg > summary.meta::-webkit-details-marker {{ display: none; }}
    details.msg[open] > summary.meta {{ border-bottom-color: #ececec; }}
    .msg-body {{ padding: 10px; }}
    .msg.role-system {{ border-color: #7a7a7a; background: #f1f1f1; }}
    .msg.role-user {{ border-color: #3b82f6; background: #f5f9ff; }}
    .msg.role-assistant {{ border-color: #10b981; background: #f3fff9; }}
    .meta {{ font-size: 12px; color: #666; margin-bottom: 6px; }}
    .role {{ font-weight: 600; color: #111; }}
    .turn-label {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; color: #0a6; background: #e6fff4; border: 1px solid #b9eed4; padding: 1px 6px; border-radius: 4px; margin-left: 6px; }}
    .text {{ white-space: pre-wrap; line-height: 1.35; }}
    .reasoning-label {{ font-size: 11px; color: #7a3e00; margin: 4px 0 2px; text-transform: uppercase; letter-spacing: 0.04em; }}
    .reasoning {{ white-space: pre-wrap; line-height: 1.35; background: #fff7e6; border: 1px dashed #f0c27b; padding: 6px; border-radius: 6px; }}
    .tool-calls-label {{ font-size: 11px; color: #075985; margin: 8px 0 3px; text-transform: uppercase; letter-spacing: 0.04em; }}
    .tool-call-link {{ font-size: 12px; color: #075985; margin: 4px 0; }}
    .tool-call {{ background: #f0f9ff; border: 1px solid #bae6fd; border-radius: 6px; margin: 5px 0; padding: 7px; }}
    .tool-call-meta {{ color: #075985; font-size: 12px; overflow-wrap: anywhere; }}
    .tool-call-arguments {{ white-space: pre-wrap; line-height: 1.35; margin: 6px 0 0; }}
    .part {{ border-top: 1px dashed #ddd; margin-top: 8px; padding-top: 6px; }}
    .part-meta {{ font-size: 11px; color: #555; margin-bottom: 2px; }}
    .part-type {{ font-size: 11px; color: #333; margin-bottom: 4px; }}
    .shot {{ display: block; max-width: 100%; height: auto; border: 1px solid #ddd; margin: 6px 0; }}
    .warn {{ color: #b00; font-size: 12px; }}
    .error {{ color: #b00; padding: 8px 0; }}
    .async-group {{ border: 1px solid #d8d8d8; border-radius: 8px; background: #fff; margin: 12px 0; overflow: hidden; }}
    .async-group > summary {{ cursor: pointer; padding: 10px; font-weight: 700; background: #f2f6fc; border-bottom: 1px solid #e4e9f2; }}
    .async-body {{ padding: 8px 10px 10px 12px; border-left: 2px solid #e9eef8; }}
    .async-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; padding: 10px; }}
    .async-col {{ min-width: 0; }}
    .async-col-title {{ font-size: 12px; font-weight: 700; color: #334; margin: 2px 0 8px; text-transform: uppercase; }}
    @media (max-width: 1100px) {{
      .layout {{ display: block; }}
      .sidebar {{ position: static; width: auto; max-width: none; max-height: none; margin-bottom: 12px; }}
      .async-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="layout">
  <aside class="sidebar">
    <h1 class="title">message history files ({len(files)})</h1>
    {render_file_tree(tree, selected)}
    {('<div class="empty">No message history found under indexed dirs.</div>' if not files else '')}
  </aside>
  <main class="main">
    <div class="toolbar">
      <div class="toolbar-top">
        <div>Selected: <strong>{html.escape(selected) if selected else "(none)"}</strong></div>
        <a class="btn" href="/refresh{'?file=' + html.escape(selected, quote=True) if selected else ''}">Refresh Index</a>
        {f'<a class="btn" href="/refresh_one?file={html.escape(selected, quote=True)}">Refresh Rollout</a>' if selected else ''}
      </div>
      {f'<div class="error">{html.escape(error)}</div>' if error else ''}
    </div>
    <div class="content">
      {body_messages}
    </div>
  </main>
  </div>
</body>
</html>
"""

        content = html_doc.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


class ViewerServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, scan_root):
        super().__init__(server_address, handler_class)
        self.scan_root = scan_root
        self._files_cache = find_message_files(scan_root, include_dirs=["agent_records", "eval"])
        self.files_set = set(self._files_cache)
        self.file_tree = build_file_tree(self._files_cache)
        self._content_cache = {}

    def get_files(self):
        return self._files_cache

    def refresh_index(self):
        self._files_cache = find_message_files(self.scan_root, include_dirs=["agent_records", "eval"])
        self.files_set = set(self._files_cache)
        self.file_tree = build_file_tree(self._files_cache)
        self._content_cache.clear()

    def _rollout_scope_from_file(self, rel_path):
        parts = [p for p in rel_path.split(os.sep) if p]
        if len(parts) < 2:
            return None
        if parts[0] in ("agent_records", "eval"):
            return parts[0] + os.sep + parts[1]
        return None

    def refresh_one_rollout(self, rel_path):
        scope = self._rollout_scope_from_file(rel_path)
        if not scope:
            return
        new_scope_files = find_message_files_under(self.scan_root, scope)
        old_files = [p for p in self._files_cache if p == scope or p.startswith(scope + os.sep)]
        old_set = set(old_files)
        keep = [p for p in self._files_cache if p not in old_set]
        merged = keep + new_scope_files
        merged.sort()
        self._files_cache = merged
        self.files_set = set(self._files_cache)
        self.file_tree = build_file_tree(self._files_cache)

        # Invalidate only cache entries in this scope.
        for p in list(self._content_cache.keys()):
            if p == scope or p.startswith(scope + os.sep):
                del self._content_cache[p]

    def get_rendered_file(self, rel_path):
        cached = self._content_cache.get(rel_path)
        if cached is not None:
            return cached
        abs_path = safe_join(self.scan_root, rel_path)
        if abs_path is None or not os.path.isfile(abs_path):
            raise FileNotFoundError(rel_path)
        rendered = load_messages_html(abs_path)
        self._content_cache[rel_path] = rendered
        return rendered


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--root", default=".")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"scan root not found: {root}", file=sys.stderr)
        return 1

    server = ViewerServer(("0.0.0.0", args.port), Handler, root)
    print(f"Trajectory viewer running on http://localhost:{args.port}")
    print(f"Root: {root}")
    print("Indexed dirs: agent_records, eval")
    print(f"Indexed files: {len(server.get_files())}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
