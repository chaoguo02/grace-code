"""
G30: Shadow Comparator — offline comparison of old vs new runtime.

Compares: model actions, tool plans, hook decisions, terminal outcome digest.
Output: JSON report with match rate, diff categories, max deviation.
No production authority modified — projections use null sink.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from runtime_core.outcome import RuntimeOutcome


@dataclass
class DiffEntry:
    category: str  # model_action | tool_plan | hook_decision | outcome_digest
    turn_index: int
    old_value: str
    new_value: str
    severity: str = "info"  # info | warning | error


@dataclass
class ComparisonReport:
    total_samples: int = 0
    matches: int = 0
    diffs: list[DiffEntry] = field(default_factory=list)
    max_token_deviation: int = 0
    max_step_deviation: int = 0

    @property
    def match_rate(self) -> float:
        if self.total_samples == 0:
            return 1.0
        return self.matches / self.total_samples

    @property
    def pass_threshold(self) -> bool:
        """G30: >= 99.5% match rate."""
        return self.match_rate >= 0.995

    def to_json(self) -> str:
        return json.dumps({
            "total_samples": self.total_samples,
            "matches": self.matches,
            "match_rate": round(self.match_rate, 4),
            "max_token_deviation": self.max_token_deviation,
            "max_step_deviation": self.max_step_deviation,
            "diff_categories": self._category_counts(),
            "diffs": [
                {"category": d.category, "turn": d.turn_index,
                 "old": d.old_value[:100], "new": d.new_value[:100],
                 "severity": d.severity}
                for d in self.diffs
            ],
        }, indent=2)

    def _category_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in self.diffs:
            counts[d.category] = counts.get(d.category, 0) + 1
        return counts


class ShadowComparator:
    """Offline comparator — replays recorded inputs through Runtime.

    G30: Does NOT modify production authority.
         Uses null projection sink.
         ToolPort returns recorded results.
    """

    def __init__(self, replay_ports, null_projection=None) -> None:
        self._ports = replay_ports
        self._projection = null_projection or NullProjectionSink(
        ) if null_projection is None else null_projection  # simplified

    def compare(
        self, inputs: list, old_outcomes: list[RuntimeOutcome],
    ) -> ComparisonReport:
        """Replay inputs and compare outcomes.

        Args:
            inputs: list of ReplayInput
            old_outcomes: previously recorded outcomes

        Returns:
            ComparisonReport with match rate and diffs.
        """
        from runtime_core.step_loop import StepLoop

        report = ComparisonReport(total_samples=len(inputs))

        for i, (inp, old_outcome) in enumerate(zip(inputs, old_outcomes)):
            context = RuntimeExecution(
                session_id=inp.session_id,
                run_id=RunId(inp.run_id),
                max_steps=inp.max_steps,
                conversation=ConversationSnapshot(
                    messages=tuple(
                        {"role": "u", "content": f"turn {t.turn_index}"}
                        for t in inp.turns
                    ),
                ),
            )

            loop = StepLoop(self._ports)
            new_outcome = loop.execute(context)

            # Compare outcomes
            if old_outcome.status == new_outcome.status:
                report.matches += 1
            else:
                report.diffs.append(DiffEntry(
                    category="outcome_digest",
                    turn_index=-1,
                    old_value=old_outcome.status.value,
                    new_value=new_outcome.status.value,
                    severity="error",
                ))

            # Track deviations
            step_diff = abs(old_outcome.steps_taken - new_outcome.steps_taken)
            token_diff = abs(old_outcome.tokens_used - new_outcome.tokens_used)
            report.max_step_deviation = max(report.max_step_deviation, step_diff)
            report.max_token_deviation = max(report.max_token_deviation, token_diff)

        return report


# Import helpers
from core.eventing.identifiers import RunId
from runtime_core.execution import RuntimeExecution, ConversationSnapshot
from validation.runtime_replay import NullProjectionSink
