"""Compile trusted structured contracts into run evidence requirements."""

from __future__ import annotations

import shlex
from typing import Any, Mapping

from agent.session.run_evidence import (
    RequiredArtifact,
    RequiredToolCall,
    RunEvidenceRequirements,
)


class RunEvidenceRequirementsFactory:
    """The only compiler for Completion Guard evidence requirements."""

    @staticmethod
    def for_run(
        *,
        request_skill: str | None = None,
        skill_arguments: str = "",
        skill_metadata: dict | None = None,
        plan_contract: dict | None = None,
        delegation_plan: Any = None,
        mode_policy: Any = None,
    ) -> RunEvidenceRequirements:
        skills = {request_skill} if request_skill else set()
        mcp_servers: set[str] = set()
        tool_calls: list[RequiredToolCall] = []
        artifacts: list[RequiredArtifact] = []

        if skill_metadata:
            mcp_servers.update(
                str(value)
                for value in skill_metadata.get("mcp_dependencies", ())
                if str(value).strip()
            )
            contract = skill_metadata.get("evidence_contract", {})
            if isinstance(contract, Mapping):
                tool_calls.extend(compile_skill_tool_calls(
                    contract, skill_arguments,
                ))

        if isinstance(plan_contract, Mapping):
            for path in _string_sequence(
                plan_contract.get("deliverables")
                or plan_contract.get("write_files")
                or plan_contract.get("target_files")
                or ()
            ):
                artifacts.append(RequiredArtifact(
                    path=path,
                    must_depend_on_required_calls=bool(tool_calls),
                    require_integrity_check=True,
                ))

        if delegation_plan is not None:
            for task in tuple(getattr(delegation_plan, "tasks", ()) or ()):
                for path in _string_sequence(getattr(task, "write_files", ())):
                    artifacts.append(RequiredArtifact(
                        path=path,
                        must_depend_on_required_calls=bool(tool_calls),
                        require_integrity_check=True,
                    ))

        verification = str(
            getattr(mode_policy, "verification_requirement", "not_required")
            if mode_policy is not None else "not_required"
        )
        return RunEvidenceRequirements(
            required_skills=frozenset(skills),
            required_mcp_servers=frozenset(mcp_servers),
            required_tool_calls=tuple(_dedupe_tool_calls(tool_calls)),
            required_artifacts=tuple(_dedupe_artifacts(artifacts)),
            verification_requirement=verification,
        )


def compile_skill_tool_calls(
    contract: Mapping[str, object],
    skill_arguments: str,
) -> list[RequiredToolCall]:
    raw_calls = contract.get(
        "required-tool-calls",
        contract.get("required_tool_calls", ()),
    )
    if not isinstance(raw_calls, list):
        return []
    values = _argument_values(skill_arguments)
    compiled: list[RequiredToolCall] = []
    for raw in raw_calls:
        if not isinstance(raw, Mapping):
            continue
        tool = str(raw.get("tool", "")).strip()
        if not tool:
            continue
        static_args = raw.get("arguments", {})
        arguments = dict(static_args) if isinstance(static_args, Mapping) else {}
        foreach_name = str(
            raw.get("foreach-argument", raw.get("foreach_argument", "")),
        ).strip()
        minimum = int(raw.get("minimum-count", raw.get("minimum_count", 1)) or 1)
        if foreach_name and values:
            for value in values:
                compiled.append(RequiredToolCall(
                    tool=tool,
                    arguments_match={**arguments, foreach_name: value},
                    minimum_count=minimum,
                ))
        else:
            compiled.append(RequiredToolCall(
                tool=tool,
                arguments_match=arguments,
                minimum_count=minimum,
            ))
    return compiled


def _argument_values(arguments: str) -> list[str]:
    text = arguments.strip()
    if not text:
        return []
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    values: list[str] = []
    for token in tokens:
        values.extend(part.strip() for part in token.split(",") if part.strip())
    return list(dict.fromkeys(values))


def _string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _dedupe_tool_calls(
    requirements: list[RequiredToolCall],
) -> list[RequiredToolCall]:
    result: list[RequiredToolCall] = []
    seen: set[tuple[object, ...]] = set()
    for requirement in requirements:
        key = (
            requirement.tool,
            tuple(sorted(requirement.arguments_match.items())),
            requirement.minimum_count,
            requirement.producer_session_id,
        )
        if key not in seen:
            seen.add(key)
            result.append(requirement)
    return result


def _dedupe_artifacts(
    requirements: list[RequiredArtifact],
) -> list[RequiredArtifact]:
    result: list[RequiredArtifact] = []
    seen: set[tuple[str, bool, bool]] = set()
    for requirement in requirements:
        key = (
            requirement.path,
            requirement.must_depend_on_required_calls,
            requirement.require_integrity_check,
        )
        if key not in seen:
            seen.add(key)
            result.append(requirement)
    return result
