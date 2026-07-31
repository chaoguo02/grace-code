"""Phase 0: Generate Snip→Micro golden file for Phase 3 verification.

Captures the output of the current two-pass pipeline (SnipCompactor.snip()
then MicroCompactor.compact()) for a representative set of conversation
histories.  The golden file is hash-committed and used as a diff baseline
when Phase 3 merges Snip+Micro into StructuralCompactor.

Usage: python scripts/phase0_snip_micro_golden.py
Output: tests/golden/snip_micro_outputs.json
"""
from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

from context.compaction import SnipCompactor, MicroCompactor
from context.token_budget import estimate_tokens


# ── Test scenario factories ──────────────────────────────────────────────


def _msg(role: str, content: str, **kwargs) -> dict:
    d: dict = {"role": role, "content": content}
    d.update(kwargs)
    return d


def _make_tool_msg(tool_call_id: str, content: str, tool_name: str = "") -> dict:
    return _msg("tool", content, tool_call_id=tool_call_id, tool_name=tool_name)


def _make_assistant_with_calls(content: str, calls: list[dict]) -> dict:
    return _msg("assistant", content, tool_calls=calls)


def _make_tool_call(name: str, params: dict, call_id: str) -> dict:
    return {"name": name, "params": params, "id": call_id}


# ── Test scenarios ───────────────────────────────────────────────────────


def scenario_empty_history() -> dict:
    """Empty history should pass through unchanged."""
    return {
        "name": "empty_history",
        "input": [],
        "description": "Empty message list produces empty output",
    }


def scenario_no_compactable_tools() -> dict:
    """Only user + plain assistant messages, no tool results."""
    return {
        "name": "no_compactable_tools",
        "input": [
            _msg("user", "Hello"),
            _msg("assistant", "Hi! How can I help?"),
            _msg("user", "What is 2+2?"),
            _msg("assistant", "The answer is 4."),
        ],
        "description": "Pure conversation without tool calls",
    }


def scenario_mixed_results() -> dict:
    """Mix of compactable and non-compactable tool results."""
    messages = [
        _msg("user", "Find all Python files and check if auth.py exists"),
        _make_assistant_with_calls("Searching...", [
            _make_tool_call("Glob", {"pattern": "**/*.py"}, "call_1"),
            _make_tool_call("Read", {"file_path": "src/auth.py"}, "call_2"),
        ]),
        _make_tool_msg("call_1", "src/main.py\nsrc/auth.py\nsrc/utils.py",
                       tool_name="Glob"),
        _make_tool_msg("call_2", "def authenticate(user):\n    ...",
                       tool_name="Read"),
        _msg("assistant", "Found 3 Python files. auth.py contains authenticate function."),
        _msg("user", "Now search for TODO comments"),
        _make_assistant_with_calls("Looking for TODOs...", [
            _make_tool_call("Grep", {"pattern": "TODO"}, "call_3"),
        ]),
        _make_tool_msg("call_3", "src/main.py:10: # TODO: refactor\n"
                       "src/auth.py:5: # TODO: add tests\n"
                       "src/utils.py:42: # TODO: optimize",
                       tool_name="Grep"),
        _msg("assistant", "Found 3 TODOs across all files."),
    ]
    return {
        "name": "mixed_results",
        "input": messages,
        "description": "Glob and Grep results (compactable), Read result (compactable)",
    }


def scenario_empty_tool_results() -> dict:
    """Tool results that are empty or No matches — should be snipped."""
    messages = [
        _msg("user", "Search for non-existent function"),
        _make_assistant_with_calls("Checking...", [
            _make_tool_call("Grep", {"pattern": "nonexistent_func"}, "call_a"),
        ]),
        _make_tool_msg("call_a", "No matches found", tool_name="Grep"),
        _msg("assistant", "No results found."),
        _msg("user", "Search for something else that isn't there"),
        _make_assistant_with_calls("Trying again...", [
            _make_tool_call("Grep", {"pattern": "also_missing"}, "call_b"),
        ]),
        _make_tool_msg("call_b", "No files found", tool_name="Grep"),
        _msg("assistant", "Still nothing."),
    ]
    return {
        "name": "empty_tool_results",
        "input": messages,
        "description": "Empty/no-match results should be snipped entirely",
    }


def scenario_rejected_tool_calls() -> dict:
    """Tool calls rejected by user/policy should be snipped."""
    messages = [
        _msg("user", "Delete the temp directory"),
        _make_assistant_with_calls("Attempting delete...", [
            _make_tool_call("Bash", {"cmd": "rm -rf /tmp"}, "call_x"),
        ]),
        _make_tool_msg("call_x", "Permission denied", tool_name="Bash"),
        _msg("assistant", "That was rejected. Let me try a read instead."),
        _make_assistant_with_calls("Reading instead...", [
            _make_tool_call("Read", {"file_path": "/tmp/status.txt"}, "call_y"),
        ]),
        _make_tool_msg("call_y", "status: ready", tool_name="Read"),
        _msg("assistant", "Status is ready."),
    ]
    return {
        "name": "rejected_tool_calls",
        "input": messages,
        "description": "Permission denied results should be snipped, valid results kept",
    }


def scenario_many_old_results() -> dict:
    """Many old tool results — MicroCompact should clear old ones, keep recent 5."""
    messages = [_msg("user", "Complex analysis task")]
    for i in range(10):
        call_id = f"cmplx_{i}"
        messages.append(_make_assistant_with_calls(f"Step {i}...", [
            _make_tool_call("Read", {"file_path": f"file_{i}.py"}, call_id),
        ]))
        messages.append(_make_tool_msg(
            call_id,
            f"Content of file_{i}.py:\n{'x' * 5000}",
            tool_name="Read",
        ))
    messages.append(_msg("assistant", "Analysis complete."))
    return {
        "name": "many_old_results",
        "input": messages,
        "description": "10 Read results — MicroCompact should keep last 5, clear first 5",
    }


def scenario_parallel_tool_calls() -> dict:
    """Assistant calls multiple tools in parallel — results must stay paired."""
    messages = [
        _msg("user", "Analyze the codebase"),
        _make_assistant_with_calls("Running analysis...", [
            _make_tool_call("Glob", {"pattern": "**/*.py"}, "p1"),
            _make_tool_call("Grep", {"pattern": "import"}, "p2"),
            _make_tool_call("Read", {"file_path": "README.md"}, "p3"),
        ]),
        _make_tool_msg("p1", "src/a.py\nsrc/b.py", tool_name="Glob"),
        _make_tool_msg("p2", "src/a.py:1: import os", tool_name="Grep"),
        _make_tool_msg("p3", "# Project", tool_name="Read"),
        _msg("assistant", "Analysis done."),
    ]
    return {
        "name": "parallel_tool_calls",
        "input": messages,
        "description": "Parallel calls: Glob (snippable, empty?), Grep+Read (compactable)",
    }


def scenario_error_results() -> dict:
    """Error messages with short output should be snipped."""
    messages = [
        _msg("user", "Run a failing command"),
        _make_assistant_with_calls("Running...", [
            _make_tool_call("Bash", {"cmd": "nonexistent_binary"}, "err_1"),
        ]),
        _make_tool_msg("err_1", "Error: command not found", tool_name="Bash"),
        _msg("assistant", "Command failed."),
    ]
    return {
        "name": "error_results",
        "input": messages,
        "description": "Short error messages should be snipped",
    }


# ── All scenarios ────────────────────────────────────────────────────────

SCENARIOS = [
    scenario_empty_history,
    scenario_mixed_results,
    scenario_empty_tool_results,
    scenario_rejected_tool_calls,
    scenario_many_old_results,
    scenario_parallel_tool_calls,
    scenario_error_results,
]


# ── Main ─────────────────────────────────────────────────────────────────


def run_pipeline(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Run Snip → Micro pipeline and return (after_snip, after_micro).

    **Important**: MicroCompactor mutates its input in-place, so we must
    copy the list between passes.  SnipCompactor returns a new list.
    """
    if not messages:
        return [], []

    # Snip pass — returns new list (does not mutate input)
    snipper = SnipCompactor()
    after_snip = snipper.snip(messages)

    # Micro compact pass — mutates input in-place, so copy first
    working = list(after_snip)  # copy to preserve after_snip for return
    mc = MicroCompactor(keep_recent=5)
    after_micro = mc.compact(working)

    return after_snip, after_micro


def main() -> int:
    output_dir = Path("tests/golden")
    output_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    for scenario_fn in SCENARIOS:
        scenario = scenario_fn()
        name = scenario["name"]
        inp = scenario["input"]
        after_snip, after_micro = run_pipeline(inp)

        # Compute fingerprints
        inp_json = json.dumps(inp, ensure_ascii=True, sort_keys=True)
        snip_json = json.dumps(after_snip, ensure_ascii=True, sort_keys=True)
        micro_json = json.dumps(after_micro, ensure_ascii=True, sort_keys=True)

        inp_hash = hashlib.sha256(inp_json.encode()).hexdigest()[:16]
        after_snip_hash = hashlib.sha256(snip_json.encode()).hexdigest()[:16]
        after_micro_hash = hashlib.sha256(micro_json.encode()).hexdigest()[:16]

        tokens_in = sum(estimate_tokens(str(m.get("content", ""))) for m in inp)
        tokens_snip = sum(estimate_tokens(str(m.get("content", ""))) for m in after_snip)
        tokens_micro = sum(estimate_tokens(str(m.get("content", ""))) for m in after_micro)

        entry = {
            "scenario": name,
            "description": scenario["description"],
            "input": inp,
            "input_count": len(inp),
            "after_snip_count": len(after_snip),
            "after_micro_count": len(after_micro),
            "tokens_before": tokens_in,
            "tokens_after_snip": tokens_snip,
            "tokens_after_micro": tokens_micro,
            "input_hash": inp_hash,
            "after_snip_hash": after_snip_hash,
            "after_micro_hash": after_micro_hash,
            "after_micro_output": after_micro,
        }
        results.append(entry)
        print(f"  {name}: {len(inp)}→{len(after_snip)}→{len(after_micro)} msgs, "
              f"{tokens_in}→{tokens_micro} tokens")

    output_path = output_dir / "snip_micro_outputs.json"
    output_path.write_text(
        json.dumps(results, ensure_ascii=True, indent=2, sort_keys=True),  # ensure_ascii=True for determinism
        encoding="ascii",
    )
    print(f"\nGolden file written: {output_path} ({output_path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
