# Plan: Remove CompletionValidator and use Claude Code-style loop completion

## Goal

Replace the current CompletionValidator-based finish validation with a Claude Code-style loop completion rule:

- if the model returns tool calls, execute them and continue;
- if the model returns a terminal/final text action, treat the run as completed successfully;
- keep explicit terminal safeguards such as max steps, repeated tool failures, loop detection, user/tool blocking, and LLM errors.

This plan intentionally does not modify `memory_list` in this round.

## User-selected scope

- Completion handling scope: global removal/deprecation of `CompletionValidator`.
- `memory_list` pagination/query changes: out of scope for this round.

## Current relevant behavior

Current `ReActAgent` finish handling calls `CompletionValidator().validate(...)` before returning success. The validator can turn a final answer into `GAVE_UP`, especially for edit-intent tasks with no writes and no explicit completion declaration.

Current tests also directly assert `CompletionValidator` behavior and PlanExecute guardrails. Removing the validator globally means those tests must be updated to reflect the new Claude Code-style contract.

## Implementation steps

### 1. Remove CompletionValidator from the ReAct finish path

In `agent/core.py`, update the `ActionType.FINISH` branch:

- remove the call to `CompletionValidator().validate(...)`;
- log task complete immediately with the final summary;
- extract success memories;
- return `RunStatus.SUCCESS`.

Keep existing non-finish terminal paths unchanged:

- `ActionType.GIVE_UP` remains `GAVE_UP`;
- max steps remains `MAX_STEPS`;
- repeated tool failures remain `GAVE_UP`;
- loop detection remains `GAVE_UP`;
- LLM call failure remains `FAILED`;
- missing pytest target special handling remains as-is.

### 2. Remove or deprecate `agent/completion.py`

Because the selected scope is global removal:

- remove imports of `CompletionValidator` from production code;
- either delete `agent/completion.py` or leave a small deprecated compatibility shim only if tests/imports require a staged migration.

Preferred final state: production runtime does not depend on `CompletionValidator`.

### 3. Update V2 intent default

Even after deleting the validator, V2 should not force `auto` to `edit` because that causes unrelated edit-only reflection behavior.

In `entry/cli.py`, change V2 intent resolution from `auto -> edit` to the existing task classifier:

- explicit `--intent analysis` and `--intent edit` still win;
- `--intent auto` uses `classify_task_intent(description, "auto", backend)` or equivalent current helper signature.

This aligns V2 with the existing run path and reduces false edit-mode no-edit reflection.

### 4. Update tests that encode old CompletionValidator semantics

Remove or rewrite direct validator tests in `test_plan_mode.py`, including tests named around:

- `test_completion_validator_requires_logged_read`;
- `test_completion_validator_accepts_logged_write`;
- broad-analysis grounding tests tied directly to the validator.

For runtime tests that currently expect `GAVE_UP` only because of completion validation, update expected status to `SUCCESS` and assert the final summary is returned.

Likely updates:

- `test_analysis_plan_cannot_finish_without_reading_allowed_file`: under the new contract, a final answer without tools is completed unless blocked by a separate runtime guardrail.
- `test_edit_plan_cannot_finish_without_write`: under the selected Claude Code-style contract, a final answer without writes is completed unless the model explicitly gives up or hits another terminal safeguard.
- `test_extractor_no_extraction_on_gave_up`: adjust to use an explicit `GIVE_UP` action or another real failure path so it still verifies extraction is skipped on failures.

Keep tests that verify policy blocks at tool execution time. The new contract removes post-hoc completion validation, not tool permission enforcement.

### 5. Add V2 regression for read-only memory-style task success

Add a regression test in `tests/test_v2_runtime.py` using `MockBackend`:

- simulate a V2 build session with tool calls such as `memory_read` or a representative read tool;
- then return a final `FINISH` action with a summary;
- run with `intent="auto"` or classified analysis path;
- assert `RunStatus.SUCCESS`.

If constructing real memory tools is too heavy, test the core V2 condition with an allowed read-only tool and final answer; the important contract is that final text after tool use is success.

### 6. Add regression for V2 auto intent classification

Add a test that asserts a V2 read-only task passed with `intent_override="auto"` resolves as analysis rather than edit. Depending on test seam, this can be checked through `classify_task_intent` directly or via a small CLI helper if one is introduced.

### 7. Run targeted tests

Run targeted tests first:

```powershell
python -m pytest tests/test_v2_runtime.py test_plan_mode.py
```

Then run memory/session related tests that may be affected by success extraction:

```powershell
python -m pytest tests/test_memory_enhancements.py tests/unit/test_session_counter_behavior.py
```

Finally, if time permits, run the whole test suite:

```powershell
python -m pytest
```

## Risks and mitigations

### Risk: Losing guardrails that prevented fake edit completion

Removing `CompletionValidator` means an edit task can finish without writes and still return success if the model says it is done. This is the user-selected behavior to match Claude Code-style loop completion.

Mitigation: keep permission enforcement, loop detection, test failure reflection, missing pytest target handling, and explicit `GIVE_UP` paths intact.

### Risk: Existing tests intentionally assert stricter completion validation

Those tests will fail until updated. The test contract must be changed from “post-hoc verifier rejects incomplete work” to “loop completion is determined by model tool-use behavior and terminal safeguards.”

### Risk: Broad-analysis grounding checks disappear

Direct evidence citation enforcement currently lives in `CompletionValidator`. Removing it means broad analysis answers are no longer rejected post-hoc for missing evidence citations.

Mitigation: if evidence grounding is still desired later, reintroduce it as an opt-in analysis-mode runtime feature, not as a global completion validator.

## Out of scope

- `memory_list` query/limit/offset/pagination.
- semantic memory search behavior.
- changing the memory storage format.
- changing child session result persistence beyond status consequences from the new success semantics.
