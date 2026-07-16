"""SubmitAnalysisTool — forced output schema for analysis tasks.

Zero Trust principle: the LLM cannot "invent" report formats. Every field
it must fill is defined in a JSON Schema with required: [...]. If the LLM
omits a required field, the tool call is rejected by function calling
validation (not by prompt — by the API itself).

Key forced fields:
- unverified_claims: LLM MUST list what it's uncertain about
- incomplete_parts: LLM MUST list what it couldn't complete
- scope_files: LLM MUST list exactly which files it analyzed
"""

from __future__ import annotations

from typing import Any

from tools.base import BaseTool, ToolResult


class SubmitAnalysisTool(BaseTool):
    """Submit an analysis report with mandatory evidence fields.

    ALL fields in the schema are required. The LLM cannot call this tool
    successfully without filling every field — the API rejects incomplete
    function calls at the JSON Schema level.
    """

    is_read_only = True

    @property
    def name(self) -> str:
        return "submit_analysis"

    @property
    def description(self) -> str:
        return (
            "Submit your final analysis report. ALL fields are REQUIRED — "
            "the call will be rejected if any field is missing or empty."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": [
                "summary",
                "findings",
                "dependency_graph",
                "unverified_claims",
                "incomplete_parts",
                "scope_files",
            ],
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Executive summary of your analysis.",
                },
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["severity", "file", "description"],
                        "properties": {
                            "severity": {
                                "type": "string",
                                "enum": ["HIGH", "MEDIUM", "LOW"],
                            },
                            "file": {"type": "string"},
                            "line": {"type": "integer"},
                            "description": {"type": "string"},
                            "evidence": {
                                "type": "string",
                                "description": "REQUIRED for HIGH: the concrete exploit evidence or attack path that proves severity."
                            },
                        },
                    },
                    "description": "List of findings. Each must include severity, file, description, and evidence.",
                },
                "dependency_graph": {
                    "type": "string",
                    "description": "ASCII or text description of the dependency graph between files analyzed.",
                },
                "unverified_claims": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": (
                        "REQUIRED: List EVERY claim you are uncertain about. "
                        "If you are 100% certain of all claims, explain WHY "
                        "for each major finding. This field CANNOT be empty — "
                        "you must either list uncertainties or justify certainty."
                    ),
                },
                "incomplete_parts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": (
                        "REQUIRED: List EVERY part of the task you could NOT "
                        "complete. For example: 'Shell line counting failed on "
                        "Windows — counted lines via file_read instead'. "
                        "If you completed ALL parts, list each part and confirm completion. "
                        "This field CANNOT be empty."
                    ),
                },
                "scope_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": (
                        "REQUIRED: List exactly which files you analyzed. "
                        "Only include files within the specified scope. "
                        "If you read files outside scope, note them separately."
                    ),
                },
            },
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        """Accept the analysis submission. Runtime validates STRUCTURED FIELDS, not text.

        Zero Trust: the Runtime judges correctness. It does NOT parse natural language.
        It validates: if severity==HIGH, is the 'evidence' field non-empty?
        """
        summary = params.get("summary", "")
        findings = params.get("findings", [])
        unverified = params.get("unverified_claims", [])
        incomplete = params.get("incomplete_parts", [])
        scope = params.get("scope_files", [])

        # ── Runtime validation: HIGH severity MUST have evidence ──
        downgraded = 0
        validated_findings = []
        for f in findings:
            severity = f.get("severity", "")
            evidence = f.get("evidence", "")
            if severity == "HIGH" and (not evidence or not evidence.strip()):
                # Physical downgrade — no NLP, no guessing. Field is empty → severity drops.
                f = dict(f)
                f["severity"] = "MEDIUM"
                f["description"] = f.get("description", "") + " [DOWNGRADED by Runtime: HIGH requires non-empty 'evidence' field]"
                downgraded += 1
            validated_findings.append(f)
        findings = validated_findings

        if downgraded > 0:
            # Inject correction feedback into the output
            correction = (
                f"\n## Runtime Correction\n"
                f"**{downgraded} finding(s) downgraded from HIGH to MEDIUM**: "
                f"the 'evidence' field was empty. HIGH severity requires a concrete "
                f"exploit path or reproduction step in the 'evidence' field.\n"
            )
        else:
            correction = ""

        # Build a structured confirmation
        lines = [
            f"## Analysis Submitted",
            correction,
            f"",
            f"**Scope**: {len(scope)} files analyzed",
            f"**Findings**: {len(findings)} issues found",
            f"**Unverified claims**: {len(unverified)} flagged",
            f"**Incomplete parts**: {len(incomplete)} acknowledged",
            f"",
            f"### Summary",
            summary,
        ]

        if findings:
            lines.append(f"\n### Findings ({len(findings)})")
            for f in findings:
                lines.append(f"- [{f.get('severity', '?')}] {f.get('file', '?')}: {f.get('description', '?')[:120]}")

        if unverified:
            lines.append(f"\n### Unverified Claims ({len(unverified)})")
            for c in unverified:
                lines.append(f"- {c[:200]}")

        if incomplete:
            lines.append(f"\n### Incomplete Parts ({len(incomplete)})")
            for p in incomplete:
                lines.append(f"- {p[:200]}")

        return ToolResult(success=True, output="\n".join(lines))
