"""条件冲突检测。

两条规则的生效区间重叠时，只有它们的适用条件 *可能被同一车型同时满足*
才构成冲突。本模块对条件集合按字段做逐字段合取：

- 字符串/枚举字段（``eq`` / ``in``）：取值域求交集，交集为空即互斥；
- 有序字段（``weight_class`` 的 ``gte`` / ``lte``）：合并数值上下界；
- 只被一方约束的字段不构成限制（车型可取该值），因此不产生互斥。

例如 ``energy_type=bev`` 与 ``energy_type=hev`` 互斥（区间可重叠共存），
而 ``energy_type=bev 且 weight_class>=B`` 与 ``weight_class<=A`` 互斥。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .models import Criterion, RuleVersion

NEG_INF = float("-inf")
POS_INF = float("inf")


@dataclass
class _Domain:
    """单字段的可行域：枚举集合与数值上下界的合取。"""

    allowed: set[Any] | None = None
    lo: float = NEG_INF
    hi: float = POS_INF

    def is_satisfiable(self) -> bool:
        if self.lo > self.hi:
            return False
        if self.allowed is not None:
            survivors = set(self.allowed)
            if self.lo != NEG_INF or self.hi != POS_INF:
                survivors = {
                    v for v in survivors
                    if isinstance(v, (int, float)) and self.lo <= v <= self.hi
                }
            return bool(survivors)
        return True

    def merge(self, other: "_Domain") -> "_Domain":
        allowed: set[Any] | None
        if self.allowed is None:
            allowed = None if other.allowed is None else set(other.allowed)
        elif other.allowed is None:
            allowed = set(self.allowed)
        else:
            allowed = self.allowed & other.allowed
        return _Domain(allowed=allowed, lo=max(self.lo, other.lo), hi=min(self.hi, other.hi))


def _as_criteria(items: Iterable[Criterion | Mapping[str, Any]]) -> tuple[Criterion, ...]:
    return tuple(c if isinstance(c, Criterion) else Criterion.from_dict(c) for c in items)


def _domains(criteria: Iterable[Criterion | Mapping[str, Any]]) -> dict[str, _Domain]:
    result: dict[str, _Domain] = {}
    for c in _as_criteria(criteria):
        domain = result.setdefault(c.field, _Domain())
        if c.op == "eq":
            values = {c.value}
            if domain.allowed is None:
                domain.allowed = set(values)
            else:
                domain.allowed &= values
        elif c.op == "in":
            values = set(c.value)
            if domain.allowed is None:
                domain.allowed = set(values)
            else:
                domain.allowed &= values
        elif c.op == "gte":
            domain.lo = max(domain.lo, c.value)
        elif c.op == "lte":
            domain.hi = min(domain.hi, c.value)
        # 新运算符需要在此扩展
    return result


def internally_consistent(criteria: Iterable[Criterion]) -> str | None:
    """检查单组条件自身是否矛盾，返回矛盾描述（无矛盾返回 None）。"""
    for field_name, domain in _domains(criteria).items():
        if not domain.is_satisfiable():
            return f"字段 {field_name} 的条件自相矛盾"
    return None


def jointly_satisfiable(
    left: Iterable[Criterion], right: Iterable[Criterion]
) -> tuple[bool, str | None]:
    """两组条件是否能被同一车型同时满足。

    返回 ``(是否可同时满足, 互斥原因)``。
    """
    domains_a = _domains(left)
    domains_b = _domains(right)
    for field_name in domains_a.keys() | domains_b.keys():
        da = domains_a.get(field_name)
        db = domains_b.get(field_name)
        if da is None or db is None:
            # 只有一方约束该字段：车型可以取该值，不互斥
            continue
        merged = da.merge(db)
        if not merged.is_satisfiable():
            return False, f"字段 {field_name} 的取值域不相交"
    return True, None


def same_coverage(
    left: Iterable[Criterion], right: Iterable[Criterion]
) -> tuple[bool, str | None]:
    """两组条件是否定义完全相同的车型覆盖集合。

    替代/勘误要求覆盖等价：只能为同一批车型换参数，不得借替代扩大或
    收窄适用面（否则旧版本是否被遮蔽将因车型而异，解析不再确定）。
    """
    domains_a = _domains(left)
    domains_b = _domains(right)
    if set(domains_a) != set(domains_b):
        only_a = ", ".join(sorted(set(domains_a) - set(domains_b)))
        only_b = ", ".join(sorted(set(domains_b) - set(domains_a)))
        detail = f"约束字段不同（仅左侧：{only_a or '无'}；仅右侧：{only_b or '无'}）"
        return False, detail
    for field_name in domains_a.keys() | domains_b.keys():
        da, db = domains_a[field_name], domains_b[field_name]
        if (da.allowed or set()) != (db.allowed or set()) or da.lo != db.lo or da.hi != db.hi:
            return False, f"字段 {field_name} 的适用取值域不同"
    return True, None


def explain_overlap(left: RuleVersion, right: RuleVersion) -> Mapping[str, Any]:
    """生成两条版本区间重叠的结构化说明。"""
    lo = max(left.effective_from, right.effective_from)
    left_to = left.effective_to.isoformat() if left.effective_to else "开放"
    right_to = right.effective_to.isoformat() if right.effective_to else "开放"
    return {
        "other": right.version_id,
        "other_window": f"{right.effective_from.isoformat()} ~ {right_to}",
        "self_window": f"{left.effective_from.isoformat()} ~ {left_to}",
        "overlap_from": lo.isoformat(),
        "other_criteria": right.criteria_text(),
    }
