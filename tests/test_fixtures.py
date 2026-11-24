def test_session_fixtures_set_up_and_tear_down_in_every_shard(pytester, sharded):
    pytester.makeconftest(
        """
        import os, pytest
        @pytest.fixture(scope="session", autouse=True)
        def service():
            shard = os.environ["PYTEST_MODAL_SHARD"]
            open(f"up-{shard}", "w").close()
            yield
            open(f"down-{shard}", "w").close()
        """
    )
    pytester.makepyfile(test_a="def test_a(): pass", test_b="def test_b(): pass")
    sharded("--modal-shards=2").assert_outcomes(passed=2)
    marks = sorted(path.name for path in pytester.path.iterdir() if path.name.startswith(("up-", "down-")))
    assert marks == ["down-0", "down-1", "up-0", "up-1"]


def module_fixture(pytester):
    pytester.makeconftest(
        """
        import pytest
        @pytest.fixture(scope="module", autouse=True)
        def table():
            open("setups", "a").write("x")
        """
    )
    pytester.makepyfile("def test_1(): pass\ndef test_2(): pass\ndef test_3(): pass\ndef test_4(): pass")


def test_a_module_fixture_runs_once_while_files_stay_whole(pytester, sharded):
    module_fixture(pytester)
    sharded("--modal-shards=4").assert_outcomes(passed=4)
    assert (pytester.path / "setups").read_text() == "x"


def test_a_module_fixture_runs_in_each_shard_once_a_file_is_split(pytester, sharded):
    module_fixture(pytester)
    sharded("--modal-shards=4", "--modal-group-by=test").assert_outcomes(passed=4)
    assert (pytester.path / "setups").read_text() == "xxxx"


def test_tests_sharing_an_xdist_group_stay_together(pytester, sharded):
    pytester.makeini("[pytest]\nmarkers = xdist_group")
    body = """
        import os, pytest
        @pytest.mark.xdist_group("db")
        def test_{0}(): open("shards", "a").write(os.environ["PYTEST_MODAL_SHARD"])
        def test_free_{0}(): pass
    """
    pytester.makepyfile(test_a=body.format("a"), test_b=body.format("b"), test_c=body.format("c"))
    sharded("--modal-shards=6", "--modal-group-by=test").assert_outcomes(passed=6)
    assert len(set((pytester.path / "shards").read_text())) == 1


def test_a_failing_session_teardown_is_reported(pytester, sharded):
    pytester.makeconftest(
        """
        import pytest
        @pytest.fixture(scope="session", autouse=True)
        def service():
            yield
            raise RuntimeError("teardown broke")
        """
    )
    pytester.makepyfile("def test_one(): pass")
    result = sharded()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*teardown broke*"])
