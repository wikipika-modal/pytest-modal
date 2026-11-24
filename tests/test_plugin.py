import json
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


def test_outcomes_match_a_plain_run(suite, sharded):
    plain = suite.runpytest_subprocess()
    result = sharded("--modal-shards=2")
    assert result.parseoutcomes() == plain.parseoutcomes()
    assert result.ret == plain.ret == 1


def test_failure_text_and_captured_output_come_back(suite, sharded):
    output = sharded().stdout
    output.fnmatch_lines(["*assert (1 + 1) == 3*"])
    output.fnmatch_lines(["*hello from the shard*"])


def test_s_shows_what_each_test_wrote_when_it_finishes(pytester, sharded):
    pytester.makepyfile(
        """
        import sys
        def test_quiet(): print("printed by a passing test")
        def test_loud():
            print("printed by a", "failing test", file=sys.stderr)
            assert False
        """
    )
    assert "printed by a passing test" not in sharded().stdout.str()
    output = sharded("-s").stdout.str()
    # Once each: a failure's report does not repeat what was already shown.
    assert output.count("printed by a passing test") == output.count("printed by a failing test") == 1


@pytest.mark.parametrize("option", ["--pdb", "--trace"])
def test_a_debugger_is_refused(suite, sharded, option):
    result = sharded(option)
    result.stderr.fnmatch_lines(["*need a terminal where the test runs*"])
    assert result.ret == pytest.ExitCode.USAGE_ERROR


def test_verbose_output_names_the_shard(suite, sharded):
    sharded("-v", "--modal-shards=3").stdout.re_match_lines([r"\[modal\d\].*PASSED.*test_c\.py::test_only"])


def test_junitxml_lists_every_test(suite, sharded):
    sharded("--junitxml=out.xml")
    names = sorted(case.get("name") for case in ET.parse(suite.path / "out.xml").iter("testcase"))
    expected = ["test_pass", "test_fail", "test_param[1]", "test_param[2]", "test_skip", "test_xfail", "test_output"]
    assert names == sorted([*expected, "test_only"])


def test_selection_is_made_by_the_controller(suite, sharded):
    sharded("-k", "param or only").assert_outcomes(passed=3, deselected=5)


def test_a_path_equal_to_the_value_of_an_option_is_still_a_path(suite, sharded):
    sharded("-k", "test_c.py", "test_c.py").assert_outcomes(passed=1)
    sharded("--modal-collect=remote", "-k", "test_c.py", "test_c.py").assert_outcomes(passed=1)


@pytest.mark.parametrize("junit", [["--junitxml=reports/out.xml"], ["--junitxml", "reports/out.xml"]])
def test_options_naming_local_files_stay_with_the_controller(pytester, sharded, junit):
    pytester.makepyfile(
        """
        def test_sees_neither(request, tmp_path):
            assert request.config.option.xmlpath is None
            assert "base" not in tmp_path.parts
        """
    )
    sharded(*junit, "--basetemp=base").assert_outcomes(passed=1)
    assert len(list(ET.parse(pytester.path / "reports/out.xml").iter("testcase"))) == 1


def test_options_from_the_environment_reach_the_shards_without_the_local_files(pytester, sharded, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "--junitxml=reports/out.xml -o junit_suite_name=named")
    pytester.makepyfile(
        """
        def test_sees_one(request):
            assert request.config.option.xmlpath is None
            assert request.config.getini("junit_suite_name") == "named"
        """
    )
    sharded().assert_outcomes(passed=1)
    assert ET.parse(pytester.path / "reports/out.xml").find("testsuite").get("name") == "named"


def test_exitfirst_stops_the_session(suite, sharded):
    result = sharded("-x", "--modal-shards=1")
    result.stdout.fnmatch_lines(["*stopping after 1 failures*"])
    assert "test_output" not in result.stdout.str()


def test_durations_are_kept_for_the_next_plan(pytester, sharded):
    pytester.makepyfile("def test_one(): pass")
    sharded()
    result = pytester.runpytest_subprocess("--cache-show", "modal/*")
    result.stdout.fnmatch_lines(["*modal/durations*", "*test_one*"])


def test_a_kept_duration_counts_time_spent_around_the_test(pytester, sharded):
    pytester.makeconftest(
        """
        import time
        def pytest_runtest_logreport(report):
            if report.when == "call":
                time.sleep(0.3)
        """
    )
    pytester.makepyfile("def test_one(): pass")
    sharded().assert_outcomes(passed=1)
    kept = json.loads((pytester.path / ".pytest_cache/v/modal/durations").read_text())
    assert kept["test_a_kept_duration_counts_time_spent_around_the_test.py::test_one"] >= 0.3


def test_a_backend_that_cannot_start_is_a_usage_error(pytester, monkeypatch):
    monkeypatch.delenv("MODAL_SANDBOX_ID", raising=False)
    pytester.makepyfile("def test_one(): pass")
    result = pytester.runpytest_subprocess("--modal", "--modal-from=disk")
    result.stderr.fnmatch_lines(["*only works inside a Modal sandbox*"])
    assert "INTERNALERROR" not in result.stdout.str() + result.stderr.str()
    assert result.ret == pytest.ExitCode.USAGE_ERROR


def test_without_modal_credentials_the_run_stops_before_it_imports_the_project(pytester, monkeypatch):
    for name in ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "MODAL_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(pytester.path / "none.toml"))
    pytester.makeconftest("open('imported', 'w').close()")
    pytester.makepyfile("def test_one(): pass")
    result = pytester.runpytest_subprocess("--modal")
    result.stderr.fnmatch_lines(["*could not sign in to Modal: Token missing*", "*modal token new*--modal-from=*"])
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert not (pytester.path / "imported").exists()


def test_a_source_that_is_no_id_is_a_usage_error(pytester, sharded):
    result = sharded("--modal-from=somewhere")
    result.stderr.fnmatch_lines(["*--modal-from: expected auto, upload, disk, a sandbox id*"])
    assert result.ret == pytest.ExitCode.USAGE_ERROR


def test_modal_conf_is_loaded_by_the_controller_only(pytester, sharded):
    pytester.makepyfile(
        modal_conf="open('loaded', 'a').write('x')", test_a="def test_a(): pass", test_b="def test_b(): pass"
    )
    sharded("--modal-shards=2").assert_outcomes(passed=2)
    assert (pytester.path / "loaded").read_text() == "x"
