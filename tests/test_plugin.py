import xml.etree.ElementTree as ET

import pytest


@pytest.fixture
def suite(pytester):
    pytester.makepyfile(
        test_a="""
            import pytest
            def test_pass(): pass
            def test_fail(): assert 1 + 1 == 3
            @pytest.mark.parametrize("n", [1, 2])
            def test_param(n): pass
        """,
        test_b="""
            import pytest
            def test_skip(): pytest.skip("no")
            @pytest.mark.xfail
            def test_xfail(): assert False
            def test_output():
                print("hello from the shard")
                assert False
        """,
        test_c="def test_only(): pass",
    )
    return pytester


def sharded(pytester, *args):
    return pytester.runpytest_subprocess("--modal", "--modal-backend=local", *args)


def test_outcomes_match_a_plain_run(suite):
    plain = suite.runpytest_subprocess()
    result = sharded(suite, "--modal-shards=2")
    assert result.parseoutcomes() == plain.parseoutcomes()
    assert result.ret == plain.ret == 1


def test_failure_text_and_captured_output_come_back(suite):
    output = sharded(suite).stdout
    output.fnmatch_lines(["*assert (1 + 1) == 3*"])
    output.fnmatch_lines(["*hello from the shard*"])


def test_verbose_output_names_the_shard(suite):
    sharded(suite, "-v", "--modal-shards=3").stdout.re_match_lines([r"\[modal\d\].*PASSED.*test_c\.py::test_only"])


def test_junitxml_lists_every_test(suite):
    sharded(suite, "--junitxml=out.xml")
    names = sorted(case.get("name") for case in ET.parse(suite.path / "out.xml").iter("testcase"))
    assert names == sorted(
        [
            "test_pass",
            "test_fail",
            "test_param[1]",
            "test_param[2]",
            "test_skip",
            "test_xfail",
            "test_output",
            "test_only",
        ]
    )


def test_selection_is_made_by_the_controller(suite):
    sharded(suite, "-k", "param or only").assert_outcomes(passed=3, deselected=5)


def test_a_shard_that_dies_fails_the_tests_it_did_not_finish(pytester):
    pytester.makepyfile(
        """
        import os
        def test_first(): pass
        def test_dies(): os._exit(7)
        def test_never_runs(): pass
        """
    )
    result = sharded(pytester)
    result.assert_outcomes(passed=1, errors=2)
    result.stdout.fnmatch_lines(["*exited 7 without a result for this test*"])


def test_a_test_the_shard_does_not_collect_is_an_error(pytester):
    pytester.makepyfile(
        """
        import sys
        def test_everywhere(): pass
        if "--modal-worker" not in sys.argv:
            def test_controller_only(): pass
        """
    )
    sharded(pytester).assert_outcomes(passed=1, errors=1)


def test_exitfirst_stops_the_session(suite):
    result = sharded(suite, "-x", "--modal-shards=1")
    result.stdout.fnmatch_lines(["*stopping after 1 failures*"])
    assert "test_output" not in result.stdout.str()


def test_durations_are_kept_for_the_next_plan(pytester):
    pytester.makepyfile("def test_one(): pass")
    sharded(pytester)
    result = pytester.runpytest_subprocess("--cache-show", "modal/*")
    result.stdout.fnmatch_lines(["*modal/durations*", "*test_one*"])
