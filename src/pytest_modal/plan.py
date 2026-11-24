from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median


@dataclass
class Shard:
    index: int
    ids: list[str] = field(default_factory=list)
    weight: float = 0.0
    where: str = ""

    @property
    def paths(self) -> list[str]:
        return sorted({nodeid.split("::")[0] for nodeid in self.ids})


def plan(
    tests: list[tuple[str, str]],
    shards: int | None,
    durations: dict[str, float],
    limit: int,
    alone: frozenset[str] = frozenset(),
) -> list[Shard]:
    """Split (nodeid, group) pairs, given in collection order, into shards of similar expected time.

    A group never spans two shards, and each group named in `alone` has a shard to itself. No number of
    sandboxes finishes before the heaviest group does. With no shard count the plan fills shards to half
    that group's weight, in at most `limit` shards: a shard also pays to start, and tests cost more in a
    process that ran others. A count that is given is not held to `limit`.
    """
    default = median(durations.values()) if durations else 1.0
    members: dict[str, list[int]] = {}
    weights: dict[str, float] = {}
    for position, (nodeid, group) in enumerate(tests):
        members.setdefault(group, []).append(position)
        weights[group] = weights.get(group, 0.0) + durations.get(nodeid, default)
    apart = [group for group in members if group in alone]
    packed = sorted((group for group in members if group not in alone), key=lambda name: -weights[name])
    if shards is None:
        wanted = math.ceil(2 * sum(weights[group] for group in packed) / weights[packed[0]]) if packed else 0
        wanted = min(wanted, limit - len(apart))
    else:
        wanted = shards - len(apart)
    shared = min(max(wanted, 1), len(packed))
    bins = [Shard(index) for index in range(shared + len(apart))]
    positions: list[list[int]] = [[] for _ in bins]
    for shard, group in zip(bins[shared:], apart):
        shard.weight = weights[group]
        positions[shard.index] = members[group]
    for group in packed:
        lightest = min(bins[:shared], key=lambda shard: shard.weight)
        lightest.weight += weights[group]
        positions[lightest.index] += members[group]
    for shard, held in zip(bins, positions):
        shard.ids = [tests[position][0] for position in sorted(held)]
    return bins
