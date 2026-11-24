REMOTE_ONLY = """
import os
if "PYTEST_MODAL_SHARD" not in os.environ:
    raise ImportError("this machine lacks the test dependencies")
"""


def project(pytester):
    pytester.makeconftest(REMOTE_ONLY)
    pytester.makepyfile(
        test_a=REMOTE_ONLY + "def test_one(): pass\ndef test_two(): assert False",
        test_b=REMOTE_ONLY + "def test_three(): pass",
    )


def test_remote_collection_needs_no_local_imports(pytester, sharded):
    project(pytester)
    result = sharded("--modal-collect=remote", "--modal-shards=2")
    result.assert_outcomes(passed=2, failed=1)
    result.stdout.fnmatch_lines(["collected 3 items in *"])
    assert result.ret == 1


def test_local_collection_of_that_project_cannot_start(pytester, sharded):
    project(pytester)
    assert sharded().ret != 0


def test_remote_collection_applies_the_selection(pytester, sharded):
    project(pytester)
    sharded("--modal-collect=remote", "-k", "one or three").assert_outcomes(passed=2)


def test_remote_collect_only_lists_the_tests(pytester, sharded):
    project(pytester)
    result = sharded("--modal-collect=remote", "--collect-only")
    result.stdout.fnmatch_lines(["test_a.py::test_one", "test_a.py::test_two", "test_b.py::test_three"])
    assert "passed" not in result.stdout.str()


def test_remote_collection_errors_stop_the_run(pytester, sharded):
    project(pytester)
    pytester.makepyfile(test_bad=REMOTE_ONLY + "import a_module_that_does_not_exist")
    result = sharded("--modal-collect=remote")
    result.stdout.fnmatch_lines(["*a_module_that_does_not_exist*", "*1 error* during collection*"])
    assert "passed" not in result.stdout.str()
    assert result.ret != 0


def test_a_file_skipped_as_a_whole_is_reported_once(pytester, sharded):
    pytester.makepyfile(
        test_a="import pytest\npytest.importorskip('a_module_that_does_not_exist')\ndef test_one(): pass",
        test_b="def test_two(): pass",
    )
    plain = pytester.runpytest_subprocess().parseoutcomes()
    assert plain == {"passed": 1, "skipped": 1}
    assert sharded().parseoutcomes() == plain
    result = sharded("--modal-collect=remote", "-rs")
    assert result.parseoutcomes() == plain
    result.stdout.fnmatch_lines(["SKIPPED*test_a.py*a_module_that_does_not_exist*"])


def test_warnings_come_back_as_in_a_plain_run(pytester, sharded):
    pytester.makepyfile(
        """
        import warnings
        warnings.warn("at import")
        class Odd(UserWarning): pass
        def test_one(): warnings.warn("in a test")
        def test_two(): warnings.warn(Odd("of the project's own kind"))
        """
    )
    plain = pytester.runpytest_subprocess().parseoutcomes()
    assert plain == {"passed": 2, "warnings": 3}
    for collect in ("local", "remote"):
        result = sharded(f"--modal-collect={collect}", "--modal-shards=2", "--modal-group-by=test")
        assert result.parseoutcomes() == plain
        result.stdout.fnmatch_lines(["*UserWarning: at import*"])
        result.stdout.fnmatch_lines(["*UserWarning: in a test*"])
        result.stdout.fnmatch_lines(["*Odd: of the project's own kind*"])


def test_last_failed_works_with_remote_collection(pytester, sharded):
    project(pytester)
    sharded("--modal-collect=remote")
    sharded("--modal-collect=remote", "--lf").assert_outcomes(failed=1)


def test_modal_args_carry_options_a_conftest_defines(pytester, sharded):
    pytester.makeconftest(
        REMOTE_ONLY + "def pytest_addoption(parser): parser.addoption('--runslow', action='store_true')"
    )
    pytester.makepyfile("def test_slow(request): assert request.config.getoption('--runslow')")
    sharded("--modal-collect=remote", "--modal-args=--runslow").assert_outcomes(passed=1)
