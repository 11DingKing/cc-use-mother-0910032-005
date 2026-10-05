"""不可变领域模型。

发布产生的任何对象一经写入即不再修改。撤回、勘误、预告全部通过
"追加新版本 / 追加事件" 表达，从而保证历史核算引用的规则快照可复现。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping

# 支持的车型条件字段与运算符。车型档案（vehicle profile）中的值必须是
# 字符串或数值；布尔条件请使用 "是"/"否" 字符串。
SUPPORTED_FIELDS = {
    "energy_type": {"eq", "in"},
    "category": {"eq", "in"},
    "fuel_label": {"eq", "in"},
    "weight_class": {"eq", "in", "gte", "lte"},
    "powertrain": {"eq", "in"},
    "model_code": {"eq", "in"},
}
ORDERED_FIELDS = {"weight_class"}


@dataclass(frozen=True)
class Criterion:
    """单条车型适用条件，例如 ``weight_class >= B``。"""

    field: str
    op: str
    value: Any

    def __post_init__(self) -> None:
        if self.field not in SUPPORTED_FIELDS:
            raise ValueError(f"不支持的条件字段：{self.field}")
        if self.op not in SUPPORTED_FIELDS[self.field]:
            raise ValueError(f"字段 {self.field} 不支持运算符 {self.op}")
        if self.op == "in":
            if not isinstance(self.value, (list, tuple)) or not self.value:
                raise ValueError("in 运算符的值必须是非空列表")
        if self.op in {"gte", "lte"} and not isinstance(self.value, (int, float)):
            raise ValueError(f"{self.field} 的 {self.op} 条件必须是数值")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Criterion":
        try:
            return cls(field=raw["field"], op=raw["op"], value=raw["value"])
        except KeyError as exc:
            raise ValueError(f"条件缺少字段：{exc.args[0]}") from None

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "op": self.op, "value": self.value}

    def matches(self, vehicle: Mapping[str, Any]) -> bool:
        actual = vehicle.get(self.field)
        if actual is None:
            return False
        if self.op == "eq":
            return actual == self.value
        if self.op == "in":
            return actual in self.value
        if self.op in {"gte", "lte"}:
            if not isinstance(actual, (int, float)):
                return False
            return actual >= self.value if self.op == "gte" else actual <= self.value
        return False  # pragma: no cover - 构造时已拦截

    def describe(self) -> str:
        if self.op == "in":
            return f"{self.field} ∈ {list(self.value)}"
        symbols = {"eq": "=", "gte": "≥", "lte": "≤"}
        return f"{self.field} {symbols[self.op]} {self.value}"


@dataclass(frozen=True)
class RuleVersion:
    """一次正式发布形成的不可变规则版本。

    ``kind`` 区分常规发布、紧急勘误与未来版本预告；``supersedes`` 建立
    版本间的替代链（勘误/主动换版时由新发布指向被替代版本）。
    """

    rule_id: str
    version: int
    effective_from: date
    effective_to: date | None  # None 表示开放区间
    criteria: tuple[Criterion, ...]
    parameters: Mapping[str, Any]
    published_at: date
    signer: str
    kind: str = "regular"  # regular | errata | preview
    supersedes: str | None = None  # 被替代版本的 version_id
    basis: str | None = None  # 勘误原因 / 预告说明
    version_id: str = field(default="")
    signature: str = field(default="")

    def __post_init__(self) -> None:
        if self.kind not in {"regular", "errata", "preview"}:
            raise ValueError(f"未知发布类型：{self.kind}")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("生效截止日不能早于生效起始日")
        if not self.criteria:
            raise ValueError("规则至少需要一条适用车型条件")
        if not isinstance(self.parameters, Mapping) or not self.parameters:
            raise ValueError("基线参数不能为空")
        if not self.version_id:
            object.__setattr__(self, "version_id", f"{self.rule_id}#v{self.version}")
        if not self.signature:
            object.__setattr__(self, "signature", self._compute_signature())

    def _canonical(self) -> bytes:
        payload = {
            "rule_id": self.rule_id,
            "version": self.version,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "criteria": [c.to_dict() for c in self.criteria],
            "parameters": self.parameters,
            "published_at": self.published_at.isoformat(),
            "signer": self.signer,
            "kind": self.kind,
            "supersedes": self.supersedes,
            "basis": self.basis,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")

    def _compute_signature(self) -> str:
        """内容签名：历史核算可据此核对当时适用的参数快照。"""
        return hashlib.sha256(self._canonical()).hexdigest()[:16]

    @property
    def is_open_ended(self) -> bool:
        return self.effective_to is None

    def window_covers(self, day: date) -> bool:
        if day < self.effective_from:
            return False
        return self.effective_to is None or day <= self.effective_to

    def window_overlaps(self, other: "RuleVersion") -> bool:
        """两个生效区间是否在日历上重叠。"""
        left_to = self.effective_to or date.max
        right_to = other.effective_to or date.max
        return self.effective_from <= right_to and other.effective_from <= left_to

    def matches_vehicle(self, vehicle: Mapping[str, Any]) -> bool:
        return all(c.matches(vehicle) for c in self.criteria)

    def criteria_text(self) -> str:
        return " 且 ".join(c.describe() for c in self.criteria)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "rule_id": self.rule_id,
            "version": self.version,
            "kind": self.kind,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "criteria": [c.to_dict() for c in self.criteria],
            "parameters": dict(self.parameters),
            "published_at": self.published_at.isoformat(),
            "signer": self.signer,
            "supersedes": self.supersedes,
            "basis": self.basis,
            "signature": self.signature,
        }


@dataclass(frozen=True)
class Draft:
    """待发布草稿：发布前冲突检测的输入。"""

    rule_id: str
    effective_from: date
    effective_to: date | None
    criteria: tuple[Criterion, ...]
    parameters: Mapping[str, Any]
    kind: str = "regular"
    supersedes: str | None = None
    basis: str | None = None


@dataclass(frozen=True)
class Withdrawal:
    """撤回签署记录。只截断未来效力，不改写任何历史版本。"""

    version_id: str
    withdrawn_at: date
    effective_from: date  # 撤回自何日 00:00 起生效
    signer: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "withdrawn_at": self.withdrawn_at.isoformat(),
            "effective_from": self.effective_from.isoformat(),
            "signer": self.signer,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Event:
    """规则库追加日志中的一条事件（发布/撤回）。"""

    seq: int
    at: date
    actor: str
    action: str
    target: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at.isoformat(),
            "actor": self.actor,
            "action": self.action,
            "target": self.target,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class Resolution:
    """按日期与车型解析的结果及匹配路径解释。

    ``status`` 为 ``unique`` / ``none`` / ``ambiguous``；即使未命中，
    ``trace`` 也完整记录每个候选版本被保留或淘汰的原因。
    """

    date: date
    vehicle: Mapping[str, Any]
    trace: tuple[dict[str, Any], ...]
    considered: int
    status: str
    version: RuleVersion | None = None
    ambiguous_candidates: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return self.status == "unique"

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "vehicle": dict(self.vehicle),
            "status": self.status,
            "rule": self.version.to_dict() if self.version else None,
            "ambiguous_candidates": list(self.ambiguous_candidates),
            "considered_candidates": self.considered,
            "match_path": list(self.trace),
        }
