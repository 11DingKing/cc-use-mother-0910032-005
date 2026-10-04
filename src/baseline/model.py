"""领域模型：角色、规则版本与生命周期。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

# 契约 actors
ROLE_ENTERPRISE_FILER = "企业申报员"
ROLE_ACCOUNTANT = "核算专员"
ROLE_OPERATOR = "交易运营员"
ROLE_AUDITOR = "监管审计员"

ALL_ROLES = frozenset(
    {ROLE_ENTERPRISE_FILER, ROLE_ACCOUNTANT, ROLE_OPERATOR, ROLE_AUDITOR}
)

# 规则版本生命周期（契约 states 的规则库映射）
STATUS_DRAFT = "草稿"
STATUS_PENDING = "待核算"
STATUS_CONFIRMED = "已确认"
STATUS_SEALED = "已封存"

# 允许的生命周期迁移（“执行中”由已确认版本在生效区间内动态计算）
TRANSITIONS: dict[tuple[str, str], str] = {
    (STATUS_DRAFT, STATUS_PENDING): ROLE_ACCOUNTANT,
    (STATUS_PENDING, STATUS_DRAFT): ROLE_ACCOUNTANT,
    (STATUS_PENDING, STATUS_CONFIRMED): ROLE_AUDITOR,
    (STATUS_CONFIRMED, STATUS_SEALED): ROLE_AUDITOR,
}


def parse_date(value: str | date) -> date:
    """解析 YYYY-MM-DD；已是 date 时原样返回。"""
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"日期必须为 YYYY-MM-DD：{value!r}") from exc


def intervals_overlap(
    start_a: date, end_a: date | None, start_b: date, end_b: date | None
) -> bool:
    """半开时间轴上两个 [start, end) 区间是否重叠（None 表示开放终点）。"""
    if start_b > start_a:
        later_start = start_b
    else:
        later_start = start_a
    earlier_end: date | None
    if end_a is None:
        earlier_end = end_b
    elif end_b is None:
        earlier_end = end_a
    else:
        earlier_end = end_a if end_a < end_b else end_b
    if earlier_end is None:
        return True
    return later_start < earlier_end


@dataclass(frozen=True)
class Actor:
    """操作人。"""

    actor_id: str
    name: str
    role: str

    def __post_init__(self) -> None:
        if self.role not in ALL_ROLES:
            raise ValueError(f"未知角色：{self.role}")
        if not self.actor_id or not self.name:
            raise ValueError("操作人编号与姓名不能为空")

    def to_dict(self) -> dict[str, str]:
        return {"actor_id": self.actor_id, "name": self.name, "role": self.role}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Actor":
        return cls(
            actor_id=value["actor_id"], name=value["name"], role=value["role"]
        )


@dataclass
class RuleVersion:
    """一条基线规则的一个不可变版本快照。

    版本一旦进入 ``已确认``，其内容（参数、条件、生效区间、签署）
    即被冻结；撤回、紧急勘误与未来版本预告都只能新增版本，
    不能改写已确认版本，历史核算因此保持可复现。
    """

    rule_code: str
    version: int
    title: str
    parameters: dict[str, Any]
    conditions: list[dict[str, Any]]
    effective_start: date
    effective_end: date | None
    supersedes_version: int | None
    status: str = STATUS_DRAFT
    version_id: str = ""
    draft_owner: Actor | None = None
    signatures: list[dict[str, str]] = field(default_factory=list)
    published_at: datetime | None = None
    withdrawn_at: datetime | None = None
    withdraw_reason: str = ""
    sealed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def is_published(self) -> bool:
        """当前操作视角下是否仍在发布（未撤回、未封存）。"""
        return self.status == STATUS_CONFIRMED and self.withdrawn_at is None

    def active_on(self, day: date) -> bool:
        """版本在某日是否对核算有效（不判断车型条件）。

        撤回与封存只影响撤回/封存时点 *之后*：历史日期仍落在
        原生效区间内时版本依然有效，从而保证历史核算可复现。
        """
        if self.status not in (STATUS_CONFIRMED, STATUS_SEALED):
            return False
        if day < self.effective_start:
            return False
        if self.effective_end is not None and day >= self.effective_end:
            return False
        if self.withdrawn_at is not None and day >= self.withdrawn_at.date():
            return False
        return True

    @property
    def signature_roles(self) -> set[str]:
        return {s["role"] for s in self.signatures}

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "rule_code": self.rule_code,
            "version": self.version,
            "title": self.title,
            "parameters": self.parameters,
            "conditions": self.conditions,
            "effective_start": self.effective_start.isoformat(),
            "effective_end": self.effective_end.isoformat()
            if self.effective_end
            else None,
            "supersedes_version": self.supersedes_version,
            "status": self.status,
            "draft_owner": self.draft_owner.to_dict() if self.draft_owner else None,
            "signatures": list(self.signatures),
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "withdrawn_at": self.withdrawn_at.isoformat() if self.withdrawn_at else None,
            "withdraw_reason": self.withdraw_reason or None,
            "sealed_at": self.sealed_at.isoformat() if self.sealed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "RuleVersion":
        import json

        owner = row["draft_owner"]
        return cls(
            rule_code=row["rule_code"],
            version=row["version"],
            title=row["title"],
            parameters=json.loads(row["parameters"]),
            conditions=json.loads(row["conditions"]),
            effective_start=date.fromisoformat(row["effective_start"]),
            effective_end=date.fromisoformat(row["effective_end"])
            if row["effective_end"]
            else None,
            supersedes_version=row["supersedes_version"],
            status=row["status"],
            version_id=row["version_id"],
            draft_owner=Actor.from_dict(json.loads(owner)) if owner else None,
            signatures=json.loads(row["signatures"]),
            published_at=datetime.fromisoformat(row["published_at"])
            if row["published_at"]
            else None,
            withdrawn_at=datetime.fromisoformat(row["withdrawn_at"])
            if row["withdrawn_at"]
            else None,
            withdraw_reason=row["withdraw_reason"] or "",
            sealed_at=datetime.fromisoformat(row["sealed_at"])
            if row["sealed_at"]
            else None,
            created_at=datetime.fromisoformat(row["created_at"])
            if row["created_at"]
            else None,
            updated_at=datetime.fromisoformat(row["updated_at"])
            if row["updated_at"]
            else None,
        )


def lifecycle_state(version: RuleVersion, day: date) -> str:
    """计算契约五态：草稿 / 待核算 / 已确认 / 执行中 / 已封存。

    “执行中”是已确认且未撤回版本落在生效区间内的动态视图，
    历史日期永远映射到当时的状态，不受后续撤回或勘误影响。
    """
    if version.status == STATUS_SEALED:
        # 封存是撤回之后的终态；按历史日期回看仍呈现当时事实
        if version.active_on(day):
            return "执行中"
        return STATUS_SEALED
    if version.status == STATUS_DRAFT:
        return STATUS_DRAFT
    if version.status == STATUS_PENDING:
        return STATUS_PENDING
    # 已确认（可能已撤回）
    if version.active_on(day):
        return "执行中"
    return STATUS_CONFIRMED
