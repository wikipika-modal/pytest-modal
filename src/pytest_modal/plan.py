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


def plan(tests: list[tuple[str, str]], shards: int | None, durations: dict[str, float], limit: int) -> list[Shard]:
    """Split (nodeid, group) pairs, given in collection order, into shards of similar expected time.

    A group never spans two shards. With no shard count the plan stops adding shards once the heaviest
    group would be the longest one, since more sandboxes could not finish sooner.
    """
    default = median(durations.values()) if durations else 1.0
    members: dict[str, list[int]] = {}
    weights: dict[str, float] = {}
    for position, (nodeid, group) in enumerate(tests):
        members.setdefault(group, []).append(position)
        weights[group] = weights.get(group, 0.0) + durations.get(nodeid, default)
    if shards is None:
        shards = math.ceil(sum(weights.values()) / max(weights.values()))
    bins = [Shard(index) for index in range(max(1, min(shards, len(members), limit)))]
    positions: list[list[int]] = [[] for _ in bins]
    for group in sorted(members, key=lambda name: -weights[name]):
        lightest = min(bins, key=lambda shard: shard.weight)
        lightest.weight += weights[group]
        positions[lightest.index] += members[group]
    for shard, held in zip(bins, positions):
        shard.ids = [tests[position][0] for position in sorted(held)]
    return bins
