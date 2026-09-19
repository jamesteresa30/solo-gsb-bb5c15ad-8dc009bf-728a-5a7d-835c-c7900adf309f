"""请求/响应模型与输入校验。"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .rfc1982 import MAX_SERIAL, classify

Serial = Annotated[int, Field(ge=0, le=MAX_SERIAL, description="32 位无符号序列号")]

# DNS 区域名：标签由字母/数字/连字符组成，允许末尾根点
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_ZONE_RE = re.compile(rf"^(?:{_LABEL}\.)+{_LABEL}?\.?$|^{_LABEL}\.?$")


def validate_zone(value: str) -> str:
    """去除首尾空白并校验 DNS 区域名，非法时抛 ValueError。"""
    zone = value.strip()
    if not zone or not _ZONE_RE.match(zone):
        raise ValueError("非法的 DNS 区域名")
    return zone


class AuditCreate(BaseModel):
    """提交一次审计：区域名 + 按观测时间升序排列的序列号列表。"""

    zone: str = Field(min_length=1, max_length=253, examples=["example.com"])
    serials: list[Serial] = Field(
        min_length=1,
        max_length=4096,
        description="按观测时间从早到晚排列的 SOA serial",
        examples=[[2026091800, 2026091801, 2026091801, 2026091705]],
    )

    @field_validator("zone")
    @classmethod
    def _check_zone(cls, value: str) -> str:
        return validate_zone(value)


class AuditItem(BaseModel):
    """单个观测点的判定结果。"""

    index: int
    serial: int
    relation: Literal["initial", "advance", "equal", "regression", "incomparable"]
    delta: int | None = Field(
        description="相对上一观测值的带符号距离；首项与不可比较时为 null"
    )
    slave_reject_risk: bool = Field(
        description="该节点是否可能使从服务器拒绝/无法应用更新"
    )
    note: str


class AuditSummary(BaseModel):
    total: int
    advance: int
    equal: int
    regression: int
    incomparable: int
    risk_indexes: list[int] = Field(description="存在从服务器拒绝风险的观测点下标")


class AuditOut(BaseModel):
    id: int
    zone: str
    created_at: str
    serials: list[int]
    items: list[AuditItem]
    summary: AuditSummary


class AuditListItem(BaseModel):
    id: int
    zone: str
    created_at: str
    total: int
    risk_count: int


class RepairPlanItem(BaseModel):
    """修复预演中单个观测点的处理结果。"""

    index: int
    original: int = Field(description="原始观测序列号")
    effective: int = Field(description="修复后生效的序列号（32 位无符号，模 2^32 回绕）")
    action: Literal["keep", "replace"] = Field(description="keep=保留原值，replace=替换为上一修复后值加 1")
    reason: str


class RepairPlanSummary(BaseModel):
    total: int
    replaced: int = Field(description="被替换的观测点数量")


class RepairPlanOut(BaseModel):
    id: int
    zone: str
    created_at: str
    items: list[RepairPlanItem]
    summary: RepairPlanSummary


class NextSerialOut(BaseModel):
    current: int
    next: int
    date: str = Field(description="建议所基于的日期（UTC，YYYYMMDD）")
    note: str


class SerialReservationCreate(BaseModel):
    """序列号预留请求：当前 serial + 幂等键。"""

    current: Serial = Field(
        description="调用方已持有的最新 serial（区域首次预留时为其当前发布值）"
    )
    request_id: str = Field(
        min_length=1,
        max_length=255,
        description="幂等键：同一区域内相同 request_id 的重试返回首次预留结果",
        examples=["deploy-20260919-001"],
    )

    @field_validator("request_id")
    @classmethod
    def _check_request_id(cls, value: str) -> str:
        request_id = value.strip()
        if not request_id:
            raise ValueError("request_id 不能为空")
        return request_id


class SerialReservationOut(BaseModel):
    id: int
    zone: str
    request_id: str
    current: int = Field(description="首次请求提交的 current 值")
    serial: int = Field(description="服务端原子分配并持久化的下一 serial")
    date: str = Field(description="分配所基于的日期（UTC，YYYYMMDD）")
    created_at: str
    replayed: bool = Field(
        default=False, description="true 表示本次为幂等重试，返回的是首次预留结果"
    )


class ReleasePlanNode(BaseModel):
    """多权威发布规划中的一个节点及其当前 serial。"""

    name: str = Field(
        min_length=1, max_length=253, examples=["ns1.example.com"], description="节点名（请求内唯一）"
    )
    serial: Serial = Field(description="节点当前持有的 SOA serial（沿用 32 位无符号校验）")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("节点名不能为空")
        return name


class ReleasePlanCreate(BaseModel):
    """多权威节点共同发布规划请求（仅规划，不写库）。"""

    nodes: list[ReleasePlanNode] = Field(
        min_length=1,
        max_length=256,
        description="参与本次发布的权威节点及其当前 serial（节点名须唯一）",
    )
    today: date | None = Field(
        default=None,
        description="规划所基于的日期（YYYY-MM-DD），缺省为服务器 UTC 当日",
    )

    @model_validator(mode="after")
    def _check_unique_names(self) -> "ReleasePlanCreate":
        seen: set[str] = set()
        duplicates: list[str] = []
        for node in self.nodes:
            if node.name in seen and node.name not in duplicates:
                duplicates.append(node.name)
            seen.add(node.name)
        if duplicates:
            raise ValueError(f"节点名须唯一，重复节点名: {', '.join(duplicates)}")
        return self


class ReleasePlanNodeResult(BaseModel):
    """候选 serial 相对单个节点的判定。"""

    name: str
    serial: int = Field(description="节点当前 serial")
    relation: Literal["advance"] = Field(description="候选相对该节点的关系（共同候选必为有效前进）")
    forward_distance: int = Field(
        ge=1, le=2**31 - 1, description="从节点当前 serial 到候选值的模 2^32 前向距离"
    )


class CandidateWindow(BaseModel):
    start: int = Field(description="当日候选区间起点 YYYYMMDD00")
    end: int = Field(description="当日候选区间终点 YYYYMMDD99")


class ReleasePlanOut(BaseModel):
    zone: str
    date: str = Field(description="规划所基于的日期（YYYYMMDD）")
    candidate: int = Field(description="候选区间中首个对所有节点均为有效前进的 serial")
    candidate_window: CandidateWindow
    nodes: list[ReleasePlanNodeResult] = Field(
        description="候选 serial 相对每个节点的关系与前向距离（与请求顺序一致）"
    )
    note: str


class ReleasePlanBlocker(BaseModel):
    """无共同候选时单个节点对当日候选区间的阻断摘要。"""

    name: str
    serial: int = Field(description="节点当前 serial")
    advance: int = Field(ge=0, le=100, description="候选区间中对该节点为有效前进的值数")
    equal: int = Field(ge=0, le=100, description="候选区间中与该节点相等的值数")
    regression: int = Field(ge=0, le=100, description="候选区间中对该节点为倒退的值数")
    incomparable: int = Field(ge=0, le=100, description="候选区间中与该节点不可比较的值数")


class ReleasePlanConflictDetail(BaseModel):
    """无共同候选时 409 响应的 detail 结构。"""

    reason: Literal["no_common_candidate", "date_out_of_range"]
    message: str
    date: str = Field(description="规划所基于的日期（YYYYMMDD）")
    candidate_window: CandidateWindow | None = Field(
        default=None, description="当日候选区间；日期超出 32 位表达范围时为 null"
    )
    checked: int = Field(default=0, description="扫描过的候选值数量（00..99 共 100 个）")
    blockers: list[ReleasePlanBlocker] = Field(
        default_factory=list, description="各节点对候选区间的 equal/regression/incomparable 阻断摘要"
    )


class ReleasePlanConflictResponse(BaseModel):
    """无共同候选时 409 响应体（HTTPException 的标准 detail 包装）。"""

    detail: ReleasePlanConflictDetail


# ---------- 区域传送收敛预演 ----------


class TransferEdge(BaseModel):
    """有向传送边 ``from -> to``：from 为上游，to 为从其同步 serial 的下游。"""

    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(
        alias="from",
        min_length=1,
        max_length=253,
        examples=["ns1.example.com"],
        description="上游节点名（须在 nodes 中声明）",
    )
    to: str = Field(
        min_length=1,
        max_length=253,
        examples=["ns2.example.com"],
        description="下游节点名（须在 nodes 中声明；与 from 相同表示自环，按普通边处理）",
    )

    @field_validator("from_", "to")
    @classmethod
    def _check_endpoint(cls, value: str) -> str:
        endpoint = value.strip()
        if not endpoint:
            raise ValueError("边端点节点名不能为空")
        return endpoint


class TransferSimulationCreate(BaseModel):
    """区域传送收敛预演请求（仅预演，不写库）。"""

    nodes: list[ReleasePlanNode] = Field(
        min_length=1,
        max_length=256,
        description="参与传送的节点及其当前 serial（节点名去首尾空白后须在请求内唯一）",
    )
    edges: list[TransferEdge] = Field(
        default_factory=list,
        max_length=4096,
        description="有向传送边列表（from 为上游、to 为下游；重复边视为一条）",
    )
    source: str = Field(
        min_length=1,
        max_length=253,
        examples=["ns1.example.com"],
        description="第 0 轮切换到发布值的源节点名（须在 nodes 中声明）",
    )
    publish_serial: Serial = Field(
        description="源节点本次发布的 serial（相对源节点当前 serial 必须是 RFC 1982 有效前进）"
    )
    max_rounds: int = Field(
        default=64,
        ge=1,
        le=1000,
        description="第 0 轮（源节点切换）之后允许执行的最大同步轮数；达到上限仍可更新返回 409",
    )

    @field_validator("source")
    @classmethod
    def _check_source(cls, value: str) -> str:
        source = value.strip()
        if not source:
            raise ValueError("源节点名不能为空")
        return source

    @model_validator(mode="after")
    def _check_references_and_publish(self) -> "TransferSimulationCreate":
        seen: set[str] = set()
        duplicates: list[str] = []
        for node in self.nodes:
            if node.name in seen and node.name not in duplicates:
                duplicates.append(node.name)
            seen.add(node.name)
        if duplicates:
            raise ValueError(f"节点名须唯一，重复节点名: {', '.join(duplicates)}")

        if self.source not in seen:
            raise ValueError(f"源节点 {self.source!r} 未在 nodes 中声明")

        unknown: list[str] = []
        for edge in self.edges:
            for endpoint in (edge.from_, edge.to):
                if endpoint not in seen and endpoint not in unknown:
                    unknown.append(endpoint)
        if unknown:
            raise ValueError(f"传送边引用了未声明的节点: {', '.join(unknown)}")

        source_serial = next(node.serial for node in self.nodes if node.name == self.source)
        relation = classify(source_serial, self.publish_serial)
        if relation != "advance":
            raise ValueError(
                f"发布 serial {self.publish_serial} 相对源节点 {self.source} 当前值 "
                f"{source_serial} 不是 RFC 1982 有效前进（判定为 {relation}）"
            )
        return self


class TransferNodeChange(BaseModel):
    """单个节点在某一轮的 serial 变更。"""

    node: str = Field(description="发生变更的节点名")
    previous_serial: int = Field(description="本轮开始时（上一轮快照）该节点持有的 serial")
    serial: int = Field(description="本轮结束后该节点采用的 serial")
    upstream: str | None = Field(
        description="该值来自的上游节点名；第 0 轮源节点切换为 null"
    )
    forward_distance: int = Field(
        ge=1,
        le=2**31 - 1,
        description="从 previous_serial 到 serial 的模 2^32 前向距离（被选中的更新必为有效前进）",
    )


class TransferRound(BaseModel):
    """一个同步轮次内的全部变更（第 0 轮恒为源节点单点切换）。"""

    round: int = Field(ge=0, description="轮次编号，0 为源节点切换轮，1 起为同步轮")
    changes: list[TransferNodeChange] = Field(
        description="本轮内同时生效的变更（同步轮按节点名字典序排列）"
    )


class TransferSnapshotNode(BaseModel):
    """快照中的单个节点及其 serial。"""

    name: str
    serial: int


class TransferSimulationResultBase(BaseModel):
    """收敛预演结果（200 与 409 共用的字段）。"""

    zone: str
    source: str = Field(description="第 0 轮切换到发布值的源节点名")
    publish_serial: int = Field(description="源节点本次发布的 serial")
    max_rounds: int = Field(description="第 0 轮之后允许执行的最大同步轮数")
    sync_rounds_run: int = Field(
        ge=0, description="实际执行的同步轮数（不含第 0 轮与无更新的终止探测轮）"
    )
    converged: bool = Field(description="是否在轮次上限内达到稳定（无节点可更新）")
    rounds: list[TransferRound] = Field(
        description="逐轮变更记录（含第 0 轮；每个记录的轮次均至少有一处变更）"
    )
    snapshot: list[TransferSnapshotNode] = Field(
        description="最终（409 时为达到上限时）各节点 serial 快照，按请求节点顺序"
    )
    unconverged: list[TransferSnapshotNode] = Field(
        description="快照中 serial 尚未等于发布值的节点（按请求节点顺序）"
    )


class TransferSimulationOut(TransferSimulationResultBase):
    """正常收敛（200）响应体。"""

    note: str


class TransferSimulationConflictDetail(TransferSimulationResultBase):
    """达到同步轮次上限仍可更新时 409 响应的 detail 结构。"""

    reason: Literal["max_rounds_exceeded"]
    message: str
    updatable_nodes: list[str] = Field(
        description="达到上限后依据当前快照仍可在下一轮更新的节点名（按请求节点顺序）"
    )


class TransferSimulationConflictResponse(BaseModel):
    """达到同步轮次上限时 409 响应体（HTTPException 的标准 detail 包装）。"""

    detail: TransferSimulationConflictDetail


# ---------- 多源观测 serial 时间线重建 ----------


class SerialTimelineObservation(BaseModel):
    """单个采集源在某一时刻对区域 SOA serial 的一次观测。"""

    collector: str = Field(
        min_length=1,
        max_length=253,
        examples=["probe-hkg-1"],
        description="采集源名（去除首尾空白）；同一源在同一时间桶内仅取最新观测",
    )
    observed_at: datetime = Field(
        description="观测时间（ISO 8601；不带时区偏移的时间戳按 UTC 处理）",
        examples=["2026-09-19T08:00:00Z"],
    )
    serial: Serial = Field(description="该次观测到的 SOA serial（32 位无符号）")

    @field_validator("collector")
    @classmethod
    def _check_collector(cls, value: str) -> str:
        collector = value.strip()
        if not collector:
            raise ValueError("采集源名不能为空")
        return collector


class SerialTimelineCreate(BaseModel):
    """多源 serial 观测时间线重建请求（仅计算，不写库）。"""

    observations: list[SerialTimelineObservation] = Field(
        min_length=1,
        max_length=4096,
        description="多采集源观测列表（至少 1 条，至多 4096 条）",
    )
    bucket_seconds: int = Field(
        ge=1,
        description=(
            "时间桶大小（秒，正整数）；观测时间按 floor(observed_at / bucket_seconds) 分桶"
        ),
        examples=[300],
    )


class TimelineCandidate(BaseModel):
    """单个时间桶内的候选 serial 及其支持/异议采集源。"""

    serial: int = Field(description="桶内某采集源最新观测产生的候选 serial")
    supporters: list[str] = Field(
        description="桶内最新观测等于该 serial 的采集源（按名字典序排列）"
    )
    dissenters: list[str] = Field(
        description="桶内最新观测为其他 serial 的采集源（按名字典序排列）"
    )


class SerialTimelineBucket(TimelineCandidate):
    """时间线中一个非空时间桶的选值结果。"""

    index: int = Field(ge=0, description="非空时间桶序号（按时间从早到晚，从 0 起；不含空桶）")
    bucket_start: str = Field(description="桶起始时间（UTC ISO 8601，含时区 Z）")
    bucket_end: str = Field(description="桶结束时间（UTC ISO 8601，左闭右开）")
    candidates: list[TimelineCandidate] = Field(
        description="该桶全部候选 serial 及其支持/异议采集源（serial 升序）"
    )


class SerialTimelineOut(BaseModel):
    """正常重建（200）响应体。"""

    zone: str
    bucket_seconds: int = Field(ge=1)
    buckets: list[SerialTimelineBucket] = Field(
        description="非空时间桶列表（按时间升序），每项给出该桶选值、支持/异议采集源"
    )
    timeline: list[int] = Field(
        description="各桶选值 serial 序列，相邻项只可能是 RFC 1982 相等或有效前进"
    )
    total_support: int = Field(ge=0, description="各桶支持采集源数之和（首要优化目标）")
    changes: int = Field(
        ge=0, description="相邻桶选值不同的跳变次数（次要优化目标，越小越优）"
    )
    note: str


class TimelinePredecessorRelation(BaseModel):
    """无法延续时，候选 serial 相对上一桶某个可达选值的关系。"""

    serial: int = Field(description="上一桶可达的选值 serial")
    relation: Literal["regression", "incomparable"] = Field(
        description="候选相对该选值的关系（禁止倒退与不可比较）"
    )


class BlockedTimelineCandidate(TimelineCandidate):
    """冲突桶中无法被选中的候选 serial。"""

    predecessor_relations: list[TimelinePredecessorRelation] = Field(
        description="该候选相对上一桶每个可达选值的关系（按上一桶 serial 升序，全部为 regression/incomparable）"
    )


class TimelineBucketRef(BaseModel):
    """冲突时间桶的位置信息。"""

    index: int = Field(ge=0, description="非空时间桶序号（首个无法延续的桶）")
    bucket_start: str
    bucket_end: str


class SerialTimelineConflictDetail(BaseModel):
    """时间线无法延续时 409 响应的 detail 结构。"""

    reason: Literal["unextendable_timeline"]
    message: str
    zone: str
    bucket_seconds: int = Field(ge=1)
    conflict_bucket: TimelineBucketRef = Field(description="首个无法延续的时间桶位置")
    candidates: list[BlockedTimelineCandidate] = Field(
        description="冲突桶的全部候选及其相对上一桶可达选值的关系"
    )
    prefix_buckets: list[SerialTimelineBucket] = Field(
        description="冲突前按相同优化目标选出的最优前缀各桶结果（不含冲突桶）"
    )
    prefix_timeline: list[int] = Field(description="最优前缀各桶选值 serial 序列")
    prefix_total_support: int = Field(ge=0, description="最优前缀的总支持采集源数")
    prefix_changes: int = Field(ge=0, description="最优前缀的跳变次数")


class SerialTimelineConflictResponse(BaseModel):
    """时间线无法延续时 409 响应体（HTTPException 的标准 detail 包装）。"""

    detail: SerialTimelineConflictDetail
