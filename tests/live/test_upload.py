"""pytest --modal from a machine outside Modal: the project is uploaded and runs in the image."""

import pytest

IMAGE = """
import sys

import modal
import pytest


def pytest_modal_image(config):
    version = "{{}}.{{}}".format(*sys.version_info)
    return modal.Image.debian_slim(python_version=version).pip_install(f"pytest=={{pytest.__version__}}", {extra!r})
"""


def test_outcomes_match_a_plain_run(pytester, on_modal):
    pytester.makepyfile(
        test_a="def test_pass(): pass\ndef test_fail(): assert 1 + 1 == 3",
        test_b="import pytest\ndef test_skip(): pytest.skip('no')\ndef test_more(): pass",
    )
    plain = pytester.runpytest_subprocess()
    result = on_modal("--modal-shards=2")
    assert result.parseoutcomes() == plain.parseoutcomes()
    result.stdout.fnmatch_lines(["*assert (1 + 1) == 3*"])


def test_remote_collection_runs_tests_this_machine_cannot_import(pytester, on_modal):
    pytester.makepyfile(
        modal_conf=IMAGE.format(extra="toolz"),
        test_a="import toolz\ndef test_uses_it(): assert toolz.first([1, 2]) == 1",
    )
    assert on_modal().ret != 0
    on_modal("--modal-collect=remote").assert_outcomes(passed=1)


def test_coverage_matches_a_plain_run(pytester, on_modal):
    # No image hook: pytest-cov reaches the shards because the default image has this environment's packages.
    pytester.makepyfile(
        pkg="def a():\n    return 1\ndef b():\n    return 2\ndef c():\n    return 3\n",
        test_a="import pkg\ndef test_a(): assert pkg.a() == 1",
        test_b="import pkg\ndef test_b(): assert pkg.b() == 2",
    )

    def totals(result):
        return [line for line in result.stdout.lines if line.startswith("TOTAL")]

    plain = totals(pytester.runpytest_subprocess("--cov=pkg", "--cov-report=term"))
    assert plain and totals(on_modal("--cov=pkg", "--cov-report=term", "--modal-shards=2")) == plain


@pytest.mark.parametrize("split", ["file", "test"])
def test_session_fixtures_run_in_every_sandbox(pytester, on_modal, split):
    pytester.makeconftest(
        """
        import os, pytest
        @pytest.fixture(scope="session")
        def sandbox():
            return os.environ["MODAL_SANDBOX_ID"]
        """
    )
    pytester.makepyfile(
        test_a="def test_a(sandbox, record_property): record_property('sandbox', sandbox)",
        test_b="def test_b(sandbox, record_property): record_property('sandbox', sandbox)",
    )
    on_modal("--modal-shards=2", f"--modal-group-by={split}", "--junitxml=out.xml").assert_outcomes(passed=2)
    import xml.etree.ElementTree as ET

    seen = {prop.get("value") for prop in ET.parse(pytester.path / "out.xml").iter("property")}
    assert len(seen) == 2


def test_weights_are_kept_on_modal_for_a_machine_that_keeps_none(pytester, on_modal):
    import modal

    pytester.makepyfile(test_a="def test_a(): pass")
    result = on_modal("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["modal: 1 shards, *, app ap-*"])
    assert "test_a.py::test_a" in modal.Dict.from_name("pytest-modal")[pytester.path.name]
