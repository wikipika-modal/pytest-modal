import pytest


def totals(result):
    return [line for line in result.stdout.lines if line.startswith("TOTAL")]


@pytest.fixture
def package(pytester):
    pytester.makepyfile(
        pkg="def a():\n    return 1\ndef b():\n    return 2\ndef c():\n    return 3\n",
        test_a="import pkg\ndef test_a(): assert pkg.a() == 1",
        test_b="import pkg\ndef test_b(): assert pkg.b() == 2",
    )
    return pytester


@pytest.mark.parametrize("collect", ["local", "remote"])
def test_coverage_matches_a_plain_run(package, sharded, collect):
    plain = totals(package.runpytest_subprocess("--cov=pkg", "--cov-report=term"))
    result = sharded("--cov=pkg", "--cov-report=term", "--modal-shards=2", f"--modal-collect={collect}")
    assert plain and totals(result) == plain


def test_reports_are_written_once_by_the_controller(package, sharded):
    package.makeconftest(
        """
        import os
        def pytest_sessionfinish(session):
            if "PYTEST_MODAL_SHARD" in os.environ:
                assert set(session.config.option.cov_report) == {""}
                open("shard-finished", "w").close()
        """
    )
    sharded("--cov=pkg", "--cov-report=xml:reports/cov.xml", "--cov-report", "term").assert_outcomes(passed=2)
    assert (package.path / "reports/cov.xml").exists() and (package.path / "shard-finished").exists()


def test_fail_under_judges_the_combined_coverage(package, sharded):
    assert sharded("--cov=pkg", "--cov-fail-under=80", "--modal-shards=2").ret == 0
    assert sharded("--cov=pkg", "--cov-fail-under=90", "--modal-shards=2").ret != 0
