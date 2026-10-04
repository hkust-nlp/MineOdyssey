"""Self-reward grading prompt template.

Owns the full text of the prompt sent to the out-of-band grader LLM
(extracted from agent.py so the prompt is editable in one place).

`build_prompt(...)` substitutes runtime values into _TEMPLATE and returns
the final user-message text. The score is always ternary:
-1 (harmful), 0 (neutral/no observable effect), 1 (beneficial).

Two modes (selected by `share_system_prompt`):
- share_system_prompt=True (cache-friendly default): the grader reuses the
  agent's own system prompt so the prefix matches the agent's prior LLM call
  and KV cache stays warm. A role-switch block is prepended to this user
  message to flip the model into third-party-grader mode.
- share_system_prompt=False: the grader call uses `GRADER_SYSTEM_PROMPT` as
  its system message instead. The role-switch block is omitted (the system
  prompt already does that job).

Placeholder syntax is `string.Template` `$name` (no f-string brace soup —
the prompt body contains JSON, which would otherwise need `{{`/`}}` escaping).
"""

from string import Template


GRADER_SYSTEM_PROMPT = """You are grading a Minecraft bot trajectory window.

You will first see the original agent's message/action sequence for this window.
After that, you will see a grading prompt containing the scoring rules and the
required output format. Follow that grading prompt to evaluate the original
agent's actions."""


_ROLE_SWITCH_BLOCK = (
    "# Role switch\n"
    "For this message only, switch perspective: you are an INDEPENDENT third-party grader. "
    "Ignore the agent persona and instructions from the system prompt above — you did NOT "
    "produce the assistant messages in this conversation. Judge them as an outsider, with no "
    "loyalty to the agent. Score honestly; do NOT inflate scores to favor \"your\" prior actions.\n"
    "\n"
)


_TEMPLATE = Template("""[Window $window_index self-reward grading — trigger `$trigger`, started $timestamp]

${role_switch_block}The agent produced $n_responses assistant response(s) in this window. Give your reason based on agent action and observation that triggered each score.

$state_snippet# Scoring rules

<general_criteria>
$general_criteria
</general_criteria>

$specific_criteria_block# Your task

**1. Decide if the active specific_criteria still fits.** Look at the actions, observations and deltas in this window. If the active spec would still distinguish 1, 0, and -1 correctly for this window, output `"specific_criteria": null` (it carries over). If it would make scoring misleading or incomplete, output a new ternary spec for this task context: explicitly describe what should count as 1, what should count as 0, and what should count as -1. $spec_example Don't write a permissive spec to inflate scores — score honestly even after emitting a new rule. Spec is for description, not curving.

**2. Score every turn** using general_criteria + the spec you committed to in step 1. Consider the whole window before assigning per-turn scores; you may use later observations or deltas to assign credit or blame to earlier turns when the dependency is clear. Each `reason` should briefly explain why the turn deserves that score, based on the available actions, observations, images, and state deltas when relevant.
One JSON object, no prose before/after, no `<action>` tag:
```json
{
  "specific_criteria": null,
  "rewards": [
    {"turn": 0, "score": $json_first, "reason": "..."},
    ...
    {"turn": $last_turn, "score": $json_last, "reason": "..."}
  ],
  "overall": {"score": $json_overall, "reason": "<one or two sentences>"}
}
```
All $n_responses turns in order, using zero-based turn ids from 0 to $last_turn. `score` must be one of -1, 0, or 1 only (no other values, no decimals). Use `1` for positive scores. `overall.score` is on the SAME ternary scale as per-turn (it's a single representative judgment of the window, NOT a sum across turns).""")


GENERAL_CRITERIA = (
    "Use a ternary scale for every turn:\n"
    "The examples below are illustrative, not exhaustive; adapt the judgment to "
    "the current task, scene, and evidence in the window.\n"
    "State deltas are authoritative for concrete state changes, but they are not "
    "exhaustive. Visual observations/images can justify scores when they reveal "
    "task-relevant opportunities, risks, or information not captured by state.\n"
    "1: beneficial. The turn produced concrete progress or a useful result, such as "
    "a useful position change, intended inventory gain/use, progress toward the task, "
    "hp/food preserved through a real risk, or visual discovery of a valuable target, "
    "safe route, escape option, or other task-relevant opportunity.\n"
    "0: neutral, unclear, or isolated no-effect. The turn had no clear observable "
    "benefit or harm, such as one redundant look, one no-op, one blocked move, one "
    "informational action, or a reasonable attempt whose outcome is not yet visible.\n"
    "-1: harmful or wasteful. The turn caused damage, death, lost inventory, worse "
    "position, lost progress, an invalid/destructive action, missed/created a visible "
    "danger, or clear repeated waste "
    "(for example the same or near-identical no-effect action repeated at least 3 "
    "times in this window, or at least 4 consecutive no-change turns). For repeated "
    "waste, mark the last turn in the streak as -1, not every turn in it."
)

SPEC_EXAMPLE = (
    "Example: 'When trapped in a 1-2 block pit, 1 for placing a block below to "
    "pillar up or mining a side wall to create an opening; 0 for an isolated view "
    "adjustment or blocked move before enough evidence shows the strategy is stuck; "
    "-1 for the 3rd+ consecutive look/move without escape progress, taking damage, "
    "or placing a block that obstructs escape.'"
)


def build_prompt(
    *,
    window_index: int,
    n_responses: int,
    trigger: str,
    state_snippet: str,
    timestamp: str,
    specific_criteria_block: str,
    share_system_prompt: bool,
) -> str:
    """Render the grading prompt by substituting runtime values into the template.

    Args:
        window_index: 1-based grading window number.
        n_responses: number of assistant turns to grade in this window.
        trigger: window-end reason ("trim_round", "rollout_end", "hard_reset", etc.).
        state_snippet: pre-formatted block containing the JSON state record
            (window_initial + per_turn_deltas + window_final). MUST end with "\\n\\n".
        timestamp: human-readable start time of the grading call.
        specific_criteria_block: pre-rendered `<specific_criteria>...</specific_criteria>`
            block including trailing "\\n\\n".
        share_system_prompt: when True, the grader call reuses the agent's own
            system prompt (cache-friendly); the role-switch block is prepended
            to this user message so the model still flips into grader mode.
            When False, the caller supplies `GRADER_SYSTEM_PROMPT` as system
            and the role-switch block is omitted.
    """
    role_switch_block = _ROLE_SWITCH_BLOCK if share_system_prompt else ""
    return _TEMPLATE.substitute(
        window_index=window_index,
        n_responses=n_responses,
        trigger=trigger,
        timestamp=timestamp,
        role_switch_block=role_switch_block,
        state_snippet=state_snippet,
        specific_criteria_block=specific_criteria_block,
        general_criteria=GENERAL_CRITERIA,
        spec_example=SPEC_EXAMPLE,
        json_first="-1",
        json_last="1",
        json_overall="0",
        last_turn=max(n_responses - 1, 0),
    )
