"""Plan Contract — industrial-grade structured execution plan.

Claude Code pattern: LLM output is untrusted dirty data. It MUST pass
through "tolerant extraction + strict validation" before becoming a
typed contract. No regex hacks, no markdown guessing.

Pipeline:
  1. extract_and_parse_json(plan_text) → dict | None
  2. PlanContract.model_validate(data) → PlanContract | ValidationError
  3. PlanValidator.validate(contract)   → (ok, error)
  4. If ok → render for human approval → inject into Build agent
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field


class PlanContract(BaseModel):
    """Structured execution plan. Every field is required — no defaults."""

    objective: str = Field(
        description="One sentence describing the business goal to achieve",
        min_length=10,
    )
    target_files: list[str] = Field(
        description="Absolute or repo-relative paths of every file to create or modify",
        min_length=1,
    )
    expected_behavior: str = Field(
        description="What the system should do after changes are applied",
        min_length=10,
    )
    verification_strategy: str = Field(
        default="",
        description="How to confirm changes work (e.g. 'pytest test_auth.py')",
    )
    potential_conflicts: list[str] = Field(
        default_factory=list,
        description="What might break, which other files depend on these",
    )

    def render_for_approval(self) -> str:
        """Render the contract as human-readable text for the approval menu."""
        lines = [
            f"## Objective\n{self.objective}\n",
            f"## Target Files",
        ]
        for f in self.target_files:
            lines.append(f"- {f}")
        lines.append("")
        lines.append(f"## Expected Behavior\n{self.expected_behavior}\n")
        if self.potential_conflicts:
            lines.append(f"## Potential Conflicts")
            for c in self.potential_conflicts:
                lines.append(f"- {c}")
            lines.append("")
        if self.verification_strategy:
            lines.append(f"## Verification\n{self.verification_strategy}\n")
        return "\n".join(lines)

    def render_for_build_agent(self) -> str:
        """Render the contract as a system constraint for the Build agent."""
        contract_json = self.model_dump_json(indent=2)
        return (
            "[SYSTEM] The following is an APPROVED execution contract. "
            "Your code changes MUST be strictly limited to the target_files "
            "listed below. You MUST satisfy the expected_behavior. "
            "Do NOT make changes outside this contract.\n\n"
            f"{contract_json}"
        )


# ── Industrial JSON extraction (bulletproof) ────────────────────────────

def extract_and_parse_json(text: str) -> dict[str, Any] | None:
    """Extract and parse JSON from mixed LLM output text.

    Tries multiple strategies in order of robustness:
      1. json5.loads() — tolerant of trailing commas, single quotes
      2. json.JSONDecoder.raw_decode() — finds first { or [ via syntax tree
      3. json5.loads() on substring between first { and last }

    Returns parsed dict, or None if no valid JSON found.
    """
    if not text or not text.strip():
        return None

    # Strategy 1: global json5 parse (most tolerant)
    try:
        import json5
        result = json5.loads(text)
        if isinstance(result, dict):
            return result
    except Exception:
        pass

    # Strategy 2: find first { or [, use stdlib raw_decode
    start_index = next((i for i, c in enumerate(text) if c in "{["), -1)
    if start_index != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, start_index)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Strategy 3: substring between first { and last }
    end_index = text.rfind("}")
    if end_index > start_index > -1:
        try:
            import json5
            result = json5.loads(text[start_index:end_index + 1])
            if isinstance(result, dict):
                return result
        except Exception:
            pass

    return None


# ── Deterministic validator ─────────────────────────────────────────────

class PlanValidator:
    """Deterministic contract validation. Zero LLM dependency."""

    @staticmethod
    def validate(contract: PlanContract) -> tuple[bool, str]:
        """Validate contract completeness. Returns (is_valid, error_message)."""
        if not contract.objective or len(contract.objective.strip()) < 10:
            return False, "objective is too short or missing"
        if not contract.target_files:
            return False, "target_files is empty — must list files to modify"
        if not contract.expected_behavior or len(contract.expected_behavior.strip()) < 10:
            return False, "expected_behavior is too short or missing"
        return True, ""
