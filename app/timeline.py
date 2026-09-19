"""多源观测的 serial 时间线重建（不落库）。

多个采集源（collector）在不同时刻上报同一区域的 SOA serial 观测值。
观测先按 ``bucket_seconds`` 划分到等宽时间桶（``observed_at`` 向下取整到
桶宽倍数的时刻为桶起点，仅保留含观测的桶并按时间升序处理）；同一采集源
在同一桶内只保留 ``observed_at`` **最新**的观测——若该最新时刻同时出现
不同的 serial，说明采集源数据自相矛盾，抛出
:class:`LatestObservationConflictError`（由 API 层转换为 HTTP 422）。

每个桶内各采集源的留存观测再按 serial 聚合为候选值：持有该值的采集源
为支持者，其余为异议者。随后用动态规划从每个桶中各选一个候选值，组成
一条相邻关系仅含 RFC 1982 ``equal`` / ``advance``（禁止 ``regression``
与 ``incomparable``）的时间线，目标按字典序依次为：

1. 各桶选值的采集源支持数之和最大；
2. 相邻桶选值发生跳变（即关系为 ``advance``）的次数最少；
3. 完全并列时，整条 serial 序列按字典序升序取最小。

若某个桶的所有候选相对上一桶的全部可延续值都是倒退或不可比较，则不
存在合法时间线，抛出 :class:`TimelineConflictError`（由 API 层转换为
HTTP 409），并携带冲突桶的位置与候选信息。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .rfc1982 import REL_ADVANCE, REL_EQUAL, classify

#: 409 detail 中的 reason 取值
ERR_NO_VALID_TIMELINE = "no_valid_timeline"
#: 422 detail 中的 reason 取值
ERR_CONFLICTING_LATEST = "conflicting_latest_observation"


def _iso8601(ts: int) -> str:
    """把 epoch 秒格式化为 UTC ISO 8601 字符串。"""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class LatestObservationConflictError(Exception):
    """同一采集源在同一时间桶的最新观测时刻出现不同 serial（HTTP 422）。

    :param detail: 结构化的冲突说明（作为响应体 detail 返回）。
    """

    def __init__(self, detail: dict) -> None:
        super().__init__(detail["message"])
        self.detail = detail


class TimelineConflictError(Exception):
    """无法构造仅含相等/有效前进关系的时间线（HTTP 409）。

    :param detail: 结构化的冲突说明（作为响应体 detail 返回）。
    """

    def __init__(self, detail: dict) -> None:
        super().__init__(detail["message"])
        self.detail = detail


def _bucketize(
    observations: list[tuple[str, datetime, int]], bucket_seconds: int
) -> list[tuple[int, dict[str, int]]]:
    """按时间桶聚合观测，返回按时间升序的 ``(桶序号, {采集源: serial})`` 列表。

    同一采集源在同一桶内只保留 observed_at 最新的观测；若其最新观测
    时刻对应多个不同 serial，抛 :class:`LatestObservationConflictError`。
    """
    # 桶序号 -> 采集源 -> (最新 observed_at, 该时刻上报过的 serial 集合)
    latest: dict[int, dict[str, tuple[datetime, set[int]]]] = {}
    for collector, observed_at, serial in observations:
        bucket_index = math.floor(observed_at.timestamp()) // bucket_seconds
        per_collector = latest.setdefault(bucket_index, {})
        current = per_collector.get(collector)
        if current is None or observed_at > current[0]:
            per_collector[collector] = (observed_at, {serial})
        elif observed_at == current[0]:
            current[1].add(serial)

    buckets: list[tuple[int, dict[str, int]]] = []
    for bucket_index in sorted(latest):
        kept: dict[str, int] = {}
        for collector in sorted(latest[bucket_index]):
            observed_at, serials = latest[bucket_index][collector]
            if len(serials) > 1:
                raise LatestObservationConflictError(
                    {
                        "reason": ERR_CONFLICTING_LATEST,
                        "message": (
                            f"采集源 {collector!r} 在时间桶 {len(buckets)}"
                            f"（起始 {_iso8601(bucket_index * bucket_seconds)}）的"
                            f"最新观测时刻 {observed_at.isoformat()} 同时上报了"
                            f"不同的 serial {sorted(serials)}"
                        ),
                        "collector": collector,
                        "bucket_index": len(buckets),
                        "bucket_start": _iso8601(bucket_index * bucket_seconds),
                        "observed_at": observed_at.isoformat(),
                        "serials": sorted(serials),
                    }
                )
            kept[collector] = next(iter(serials))
        buckets.append((bucket_index, kept))
    return buckets


def _build_candidates(kept: dict[str, int]) -> list[dict]:
    """把桶内各采集源留存的 serial 聚合为候选列表（按 serial 升序）。

    每个候选记录支持者（桶内最新观测为该值的采集源）与异议者（最新
    观测为其他值的采集源），均按采集源名字典序排列。
    """
    supporters_by_serial: dict[int, list[str]] = {}
    for collector, serial in kept.items():
        supporters_by_serial.setdefault(serial, []).append(collector)
    candidates: list[dict] = []
    for serial in sorted(supporters_by_serial):
        supporters = sorted(supporters_by_serial[serial])
        supporter_set = set(supporters)
        dissenters = sorted(c for c in kept if c not in supporter_set)
        candidates.append(
            {"serial": serial, "supporters": supporters, "dissenters": dissenters}
        )
    return candidates


# DP 路径形如 (支持数合计, 跳变次数, 各桶选值序列)，按目标字典序比较
def _is_better(
    path: tuple[int, int, tuple[int, ...]], best: tuple[int, int, tuple[int, ...]]
) -> bool:
    """path 是否优于 best：支持数多者优先，其次跳变少者，再次序列字典序小者。"""
    if path[0] != best[0]:
        return path[0] > best[0]
    if path[1] != best[1]:
        return path[1] < best[1]
    return path[2] < best[2]


def build_serial_timeline(
    zone: str,
    observations: list[tuple[str, datetime, int]],
    bucket_seconds: int,
) -> dict:
    """从多源观测重建一条合法 serial 时间线，返回各桶选值与支持/异议情况。

    :param observations: ``(采集源, 观测时刻, serial)`` 列表（时刻须带时区）；
    :param bucket_seconds: 时间桶宽度（秒）。
    :return: 时间线结果字典；采集源最新观测自相矛盾时抛
             :class:`LatestObservationConflictError`，无法构造合法时间线时抛
             :class:`TimelineConflictError`。
    """
    buckets = _bucketize(observations, bucket_seconds)
    candidates_per_bucket = [
        (bucket_index, _build_candidates(kept)) for bucket_index, kept in buckets
    ]

    # 动态规划：states[serial] = 以该 serial 结尾的最优路径
    # （同一状态的后续转移只取决于该 serial，故每个状态只需保留最优前缀）
    states: dict[int, tuple[int, int, tuple[int, ...]]] = {}
    for candidate in candidates_per_bucket[0][1]:
        states[candidate["serial"]] = (
            len(candidate["supporters"]),
            0,
            (candidate["serial"],),
        )

    for position in range(1, len(candidates_per_bucket)):
        bucket_index, candidates = candidates_per_bucket[position]
        next_states: dict[int, tuple[int, int, tuple[int, ...]]] = {}
        for candidate in candidates:
            serial = candidate["serial"]
            support = len(candidate["supporters"])
            best: tuple[int, int, tuple[int, ...]] | None = None
            for prev_serial, prev_path in states.items():
                relation = classify(prev_serial, serial)
                if relation not in (REL_EQUAL, REL_ADVANCE):
                    continue  # 禁止倒退与不可比较
                jump = 1 if relation == REL_ADVANCE else 0
                path = (
                    prev_path[0] + support,
                    prev_path[1] + jump,
                    prev_path[2] + (serial,),
                )
                if best is None or _is_better(path, best):
                    best = path
            if best is not None:
                next_states[serial] = best
        if not next_states:
            prev_bucket_index = candidates_per_bucket[position - 1][0]
            raise TimelineConflictError(
                {
                    "reason": ERR_NO_VALID_TIMELINE,
                    "message": (
                        f"时间桶 {position}"
                        f"（起始 {_iso8601(bucket_index * bucket_seconds)}）的所有候选"
                        f" serial 相对时间桶 {position - 1} 的可延续值均为倒退或"
                        "不可比较，无法构造仅含相等/有效前进关系的时间线"
                    ),
                    "bucket_index": position,
                    "bucket_start": _iso8601(bucket_index * bucket_seconds),
                    "candidates": candidates,
                    "previous_bucket_index": position - 1,
                    "previous_bucket_start": _iso8601(prev_bucket_index * bucket_seconds),
                    "previous_serials": sorted(states),
                }
            )
        states = next_states

    best_path: tuple[int, int, tuple[int, ...]] | None = None
    for path in states.values():
        if best_path is None or _is_better(path, best_path):
            best_path = path
    assert best_path is not None  # 至少一个观测即至少一个桶，首桶必有候选
    total_support, jumps, sequence = best_path

    buckets_out: list[dict] = []
    for position, ((bucket_index, candidates), serial) in enumerate(
        zip(candidates_per_bucket, sequence)
    ):
        selected = next(c for c in candidates if c["serial"] == serial)
        relation = (
            "initial" if position == 0 else classify(sequence[position - 1], serial)
        )
        buckets_out.append(
            {
                "index": position,
                "bucket_start": _iso8601(bucket_index * bucket_seconds),
                "serial": serial,
                "relation": relation,
                "supporters": selected["supporters"],
                "dissenters": selected["dissenters"],
                "candidates": [
                    {
                        "serial": candidate["serial"],
                        "supporters": candidate["supporters"],
                        "dissenters": candidate["dissenters"],
                        "selected": candidate["serial"] == serial,
                    }
                    for candidate in candidates
                ],
            }
        )

    return {
        "zone": zone,
        "bucket_seconds": bucket_seconds,
        "bucket_count": len(buckets_out),
        "total_support": total_support,
        "jumps": jumps,
        "buckets": buckets_out,
        "note": (
            "时间线由动态规划在各桶候选值中选出：相邻选值仅含 RFC 1982 相等或"
            "有效前进关系，依次最大化采集源支持数、最少跳变、整条序列字典序最小；"
            "结果仅用于观测分析，不写入数据库"
        ),
    }
