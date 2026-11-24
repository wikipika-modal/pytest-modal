from pytest_modal.plan import plan


def test_every_test_lands_in_exactly_one_shard_in_collection_order():
    tests = [(f"{name}.py::test_{n}", f"{name}.py") for name in "abcde" for n in range(3)]
    shards = plan(tests, 3, {}, 64)
    assert sorted(nodeid for shard in shards for nodeid in shard.ids) == sorted(nodeid for nodeid, _ in tests)
    order = {nodeid: position for position, (nodeid, _) in enumerate(tests)}
    for shard in shards:
        assert shard.ids == sorted(shard.ids, key=order.get)


def test_a_group_stays_in_one_shard():
    tests = [(f"{name}.py::test_{n}", f"{name}.py") for name in "abcd" for n in range(4)]
    for shard in plan(tests, 3, {}, 64):
        for path in shard.paths:
            assert sum(nodeid.startswith(path) for nodeid in shard.ids) == 4


def test_known_durations_balance_the_shards():
    tests = [("slow.py::test", "slow.py"), *[(f"fast{n}.py::test", f"fast{n}.py") for n in range(10)]]
    durations = {"slow.py::test": 10.0, **{f"fast{n}.py::test": 1.0 for n in range(10)}}
    shards = plan(tests, 2, durations, 64)
    assert sorted(shard.weight for shard in shards) == [10.0, 10.0]


def test_auto_stops_at_the_heaviest_group():
    tests = [(f"big.py::test_{n}", "big.py") for n in range(100)] + [
        (f"small{n}.py::test", f"small{n}.py") for n in range(9)
    ]
    assert len(plan(tests, None, {}, 64)) == 2


def test_shard_count_is_bounded_by_groups_and_the_limit():
    tests = [(f"{n}.py::test", f"{n}.py") for n in range(5)]
    assert len(plan(tests, 50, {}, 64)) == 5
    assert len(plan(tests, 50, {}, 3)) == 3
