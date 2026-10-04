# Self-reward grader: known "no reasoning / no response" cases

Out-of-band self-reward grading uses the same LLM as the action agent (currently
Kimi K2.6) via a separate chat-completion call. Per-window grades land in
`agent_records/<run>/self_reward_{N}.json`. Prompt is built by
`agent.grading_prompt.build_prompt` and dispatched by
`agent.Agent._run_out_of_band_self_reward`.

Kimi splits its output between `message.content` (final answer) and
`message.reasoning_content` (chain-of-thought). The boundary can drift: with a
tight `max_tokens` budget, reasoning consumes the budget and content arrives
truncated or empty. We have observed this on real runs (v11 w2/w4, v12 w2) and
mitigated by raising `max_tokens=32000` and shrinking `MCBOTS_MAX_CONVERSATION_ROUNDS`
to 50 (window n=20). The drift is not eliminated — it is now rarer.

Two code paths still silently lose signal in the "both empty" tail of this
distribution. Documented here, not yet patched.

## 1. Grading agent: both `content` and `reasoning_content` empty

`agent/agent.py::_run_out_of_band_self_reward` extracts both fields and
prefers `content`, falling back to `reasoning_content` only if `content` is
empty (this fallback already prints `ℹ️  ... recovered grade from
reasoning_content`).

If **both** come back empty (API returned a successful response with zero text),
the handler exits the retry loop with `response_text = ""` and
`reasoning_content = None`. The empty-string is not `None`, so the save path
fires and writes:

```json
{
  "window_index": N,
  "raw_response": "",
  "reasoning_content": null,
  "parsed": null,
  ...
}
```

No warning is printed. `parsed` is null because the regex finds no JSON in `""`.
`self_reward_window_count` still advances. From outside, the window looks
"ungraded" with no obvious signal in stderr.

To detect after the fact, scan grading files for `parsed: null` AND
`raw_response: ""` AND `reasoning_content: null`.

## 2. Action agent: `content` empty → whole turn dropped, reasoning lost

`agent/agent.py` around the main rollout loop has `_is_empty_assistant_content`.
When the action LLM returns an assistant message whose `content` is None / "" /
whitespace / empty multimodal list, the **entire turn is dropped** with
`⚠️  empty assistant message content (dropped, not appended to history)`. The
turn is not saved to `full_message_history`, so it doesn't appear in
`messages.json` and is invisible to subsequent grading windows.

This guard exists because Kimi explicitly rejects subsequent requests that
contain an empty `assistant` message in history (`position N with role
'assistant' must not be empty`). The drop is the correct behavior for that
constraint.

But if the action answer drifted entirely into `reasoning_content` and
`content` came back empty, the reasoning is also lost — the code path does not
inspect `reasoning_content` before discarding. There is no fallback symmetric
to the grading agent's content-empty branch.

Mechanism is the same as the grading drift: a sufficiently long reasoning
phase can leave content blank. Probability is low at n=20 windows but not zero.

## Why we are not patching these yet

Both cases are infrequent failure tails of the same reasoning/content drift
problem we already mitigated with `max_tokens=32000` + smaller windows. v13
saw 15/15 grading windows parse successfully. Adding fallbacks now would:

- **Grading agent both-empty**: warning is easy to add, but a true both-empty
  response has no signal to extract anyway. Patch is cosmetic.
- **Action agent reasoning fallback**: parsing reasoning_content as if it were
  an action requires re-running the same `<action>` extractor against text the
  model itself classified as "thinking, not answer". Risk of injecting
  malformed actions into the world. Needs careful design.

Revisit if v14+ shows the drift recurring at the rate we used to see at N=100.

## Pointers

- Grading prompt template: `agent/grading_prompt.py`
- Grader dispatch + response handling: `agent/agent.py::_run_out_of_band_self_reward`
- Action-agent empty-content guard: `agent/agent.py::_is_empty_assistant_content`
  (inside the main decision loop)
- Reasoning/content drift commit history reference: search `git log --all
  --grep="reasoning_content"` for the contemporaneous fixes.
