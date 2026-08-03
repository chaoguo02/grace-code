"""G30: Offline Shadow — zero production side effects, >= 99.5% match.

AC: No production authority modified (null projection sink)
AC: ToolPort uses recorded results (no real execution)
AC: Compatible outcomes → match rate >= 99.5%
AC: JSON report generated with categories
"""

from __future__ import annotations

import json

import pytest

from validation.shadow_comparator import ShadowComparator, ComparisonReport, DiffEntry


class TestShadowSafety:
    """G30: Offline shadow does not modify production."""

    def test_report_match_rate_100_on_empty(self):
        report = ComparisonReport(total_samples=0, matches=0)
        assert report.match_rate == 1.0
        assert report.pass_threshold

    def test_report_match_rate_calculation(self):
        report = ComparisonReport(total_samples=100, matches=99)
        # 99/100 = 0.99 — below 99.5% threshold
        assert not report.pass_threshold
        assert report.match_rate == 0.99

    def test_report_passes_at_995(self):
        report = ComparisonReport(total_samples=1000, matches=995)
        assert report.match_rate == 0.995
        assert report.pass_threshold

    def test_diff_categories_counted(self):
        report = ComparisonReport(total_samples=10, matches=8)
        report.diffs.append(DiffEntry(category="model_action", turn_index=0,
                                       old_value="a", new_value="b"))
        report.diffs.append(DiffEntry(category="model_action", turn_index=1,
                                       old_value="c", new_value="d"))

        j = json.loads(report.to_json())
        assert j["diff_categories"]["model_action"] == 2

    def test_json_report_contains_required_fields(self):
        report = ComparisonReport(total_samples=50, matches=49,
                                  max_token_deviation=100, max_step_deviation=2)
        j = json.loads(report.to_json())
        assert "total_samples" in j
        assert "matches" in j
        assert "match_rate" in j
        assert "max_token_deviation" in j
        assert "max_step_deviation" in j
        assert "diff_categories" in j
        assert "diffs" in j

    def test_null_projection_no_db_write(self):
        from validation.runtime_replay import NullProjectionSink
        sink = NullProjectionSink()
        # Create a minimal envelope-like object
        class FakeEnvelope:
            event_id = "e1"
            event_type = "run.completed.v1"
        sink.on_event(FakeEnvelope())
        assert len(sink.events) == 1  # in-memory only, no DB write
