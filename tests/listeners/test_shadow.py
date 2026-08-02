"""P18: Shadow Runner — acceptance tests."""

from listeners.shadow import ShadowRunner


class TestShadowRunner:

    def test_old_result_returned(self):
        sr = ShadowRunner(lambda x: "OLD", lambda x: "NEW")
        assert sr("input") == "OLD"

    def test_mismatch_counted(self):
        sr = ShadowRunner(lambda x: 1, lambda x: 2)
        sr("x")
        assert sr.mismatches == 1

    def test_match_not_counted(self):
        sr = ShadowRunner(lambda x: 42, lambda x: 42)
        sr("x")
        assert sr.mismatches == 0

    def test_new_handler_failure_survives(self):
        sr = ShadowRunner(lambda x: "ok", lambda x: (_ for _ in ()).throw(RuntimeError("boom")))
        result = sr("x")
        assert result == "ok"
        assert sr.mismatches == 0
        assert sr._new_failures == 1
