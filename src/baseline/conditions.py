"""车型适用条件引擎。

条件以 ``{"field", "operator", "value"}`` 描述，同组条件之间为 AND。
引擎负责：

1. 判定车型属性是否满足条件（并给出逐条判定轨迹，供匹配路径解释）；
2. 检测同一版本内部的自相矛盾；
3. 判定两个版本的条件是否 *可能同时命中同一车型* —— 发布前的
   区间重叠检测据此区分“真冲突”与“互斥条件下的合法分段”。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ValidationError

# 字段 -> 类型
STRING_FIELDS = frozenset({"energy_type", "vehicle_category", "brand", "model_code"})
NUMBER_FIELDS = frozenset(
    {"curb_weight_kg", "range_km", "passenger_count", "production_year"}
)

STRING_OPERATORS = frozenset({"eq", "ne", "in", "not_in"})
NUMBER_OPERATORS = frozenset({"eq", "ne", "lt", "le", "gt", "ge", "between"})


@dataclass(frozen=True)
class Condition:
    """单条车型适用条件。"""

    field: str
    operator: str
    value: Any

    def __post_init__(self) -> None:
        if self.field in STRING_FIELDS:
            if self.operator not in STRING_OPERATORS:
                raise ValidationError(
                    f"字段 {self.field} 不支持算子 {self.operator}",
                    {"field": self.field, "operator": self.operator},
                )
            self._check_string_value()
        elif self.field in NUMBER_FIELDS:
            if self.operator not in NUMBER_OPERATORS:
                raise ValidationError(
                    f"字段 {self.field} 不支持算子 {self.operator}",
                    {"field": self.field, "operator": self.operator},
                )
            self._check_number_value()
        else:
            raise ValidationError(
                f"未知车型字段：{self.field}",
                {"field": self.field, "allowed": sorted(STRING_FIELDS | NUMBER_FIELDS)},
            )

    def _check_string_value(self) -> None:
        if self.operator in ("in", "not_in"):
            if not isinstance(self.value, list) or not self.value:
                raise ValidationError(
                    f"{self.operator} 的值必须是非空列表", {"condition": self.to_dict()}
                )
            if not all(isinstance(v, str) and v for v in self.value):
                raise ValidationError(
                    f"{self.field} 列表元素必须是非空字符串",
                    {"condition": self.to_dict()},
                )
            if len(set(self.value)) != len(self.value):
                raise ValidationError(
                    f"{self.field} 列表存在重复值", {"condition": self.to_dict()}
                )
        else:
            if not isinstance(self.value, str) or not self.value:
                raise ValidationError(
                    f"{self.field} 的值必须是非空字符串", {"condition": self.to_dict()}
                )

    def _check_number_value(self) -> None:
        if self.operator == "between":
            if (
                not isinstance(self.value, list)
                or len(self.value) != 2
                or not all(isinstance(v, (int, float)) for v in self.value)
            ):
                raise ValidationError(
                    "between 的值必须是 [下限, 上限]", {"condition": self.to_dict()}
                )
            low, high = self.value
            if low > high:
                raise ValidationError(
                    "between 下限不能大于上限", {"condition": self.to_dict()}
                )
        else:
            if not isinstance(self.value, (int, float)) or isinstance(
                self.value, bool
            ):
                raise ValidationError(
                    f"{self.field} 的值必须是数值", {"condition": self.to_dict()}
                )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Condition":
        if not isinstance(raw, dict):
            raise ValidationError("条件必须是对象", {"condition": raw})
        missing = {"field", "operator", "value"} - raw.keys()
        if missing:
            raise ValidationError(
                "条件缺少字段：" + "、".join(sorted(missing)), {"condition": raw}
            )
        return cls(field=raw["field"], operator=raw["operator"], value=raw["value"])

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "operator": self.operator, "value": self.value}

    # ---- 车型匹配 ----

    def evaluate(self, vehicle: dict[str, Any]) -> tuple[bool, str]:
        """判定单条条件；返回 (是否满足, 不满足原因)。"""
        if self.field not in vehicle:
            return False, f"车型缺少属性 {self.field}"
        actual = vehicle[self.field]
        if self.field in STRING_FIELDS:
            return self._eval_string(actual)
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return False, f"{self.field} 不是数值：{actual!r}"
        return self._eval_number(actual)

    def _eval_string(self, actual: Any) -> tuple[bool, str]:
        if not isinstance(actual, str):
            return False, f"{self.field} 不是字符串：{actual!r}"
        if self.operator == "eq":
            ok = actual == self.value
        elif self.operator == "ne":
            ok = actual != self.value
        elif self.operator == "in":
            ok = actual in self.value
        else:  # not_in
            ok = actual not in self.value
        return ok, "" if ok else f"{self.field}={actual!r} 不满足 {self.operator} {self.value!r}"

    def _eval_number(self, actual: float) -> tuple[bool, str]:
        op, expected = self.operator, self.value
        if op == "eq":
            ok = actual == expected
        elif op == "ne":
            ok = actual != expected
        elif op == "lt":
            ok = actual < expected
        elif op == "le":
            ok = actual <= expected
        elif op == "gt":
            ok = actual > expected
        elif op == "ge":
            ok = actual >= expected
        else:  # between，闭区间
            low, high = expected
            ok = low <= actual <= high
        return ok, "" if ok else f"{self.field}={actual!r} 不满足 {op} {expected!r}"

    # ---- 冲突判定的域表示 ----

    def string_domain(self) -> tuple[frozenset[str] | None, frozenset[str]]:
        """字符串条件归一化为 (允许集合, 禁止集合)；允许集合 None 表示全集。"""
        if self.operator == "eq":
            return frozenset({self.value}), frozenset()
        if self.operator == "ne":
            return None, frozenset({self.value})
        if self.operator == "in":
            return frozenset(self.value), frozenset()
        return None, frozenset(self.value)  # not_in

    def number_interval(self) -> tuple[float, float, bool, bool]:
        """数值条件归一化为区间 (low, high, low_inclusive, high_inclusive)。"""
        inf = float("inf")
        op, v = self.operator, self.value
        if op == "eq":
            return float(v), float(v), True, True
        if op == "ne":
            # 连续轴上剔除单点不改变区间连通性，视为全开区间；
            # 与 eq 的直接矛盾在调用方单独处理。
            return -inf, inf, False, False
        if op == "lt":
            return -inf, float(v), False, False
        if op == "le":
            return -inf, float(v), False, True
        if op == "gt":
            return float(v), inf, False, False
        if op == "ge":
            return float(v), inf, True, False
        return float(v[0]), float(v[1]), True, True  # between 闭区间


def _intersect_intervals(
    acc: tuple[float, float, bool, bool],
    add: tuple[float, float, bool, bool],
) -> tuple[float, float, bool, bool]:
    """两个带端点包含性区间的交。"""
    low_a, high_a, lo_inc_a, hi_inc_a = acc
    low_b, high_b, lo_inc_b, hi_inc_b = add
    if low_a > low_b:
        low, lo_inc = low_a, lo_inc_a
    elif low_b > low_a:
        low, lo_inc = low_b, lo_inc_b
    else:
        low, lo_inc = low_a, lo_inc_a and lo_inc_b
    if high_a < high_b:
        high, hi_inc = high_a, hi_inc_a
    elif high_b < high_a:
        high, hi_inc = high_b, hi_inc_b
    else:
        high, hi_inc = high_a, hi_inc_a and hi_inc_b
    return low, high, lo_inc, hi_inc


def _interval_empty(interval: tuple[float, float, bool, bool]) -> bool:
    low, high, lo_inc, hi_inc = interval
    if low > high:
        return True
    if low == high and not (lo_inc and hi_inc):
        return True
    return False


def _conditions_of(raw_list: list[dict[str, Any]] | list[Condition]) -> list[Condition]:
    result = []
    for item in raw_list:
        if isinstance(item, Condition):
            result.append(item)
        else:
            result.append(Condition.from_dict(item))
    return result


def condition_matches(
    conditions: list[dict[str, Any]] | list[Condition], vehicle: dict[str, Any]
) -> tuple[bool, list[dict[str, Any]]]:
    """判定整车是否满足全部条件（AND），同时返回逐条轨迹。"""
    trace: list[dict[str, Any]] = []
    all_ok = True
    for cond in _conditions_of(conditions):
        ok, reason = cond.evaluate(vehicle)
        trace.append(
            {
                "condition": cond.to_dict(),
                "matched": ok,
                "reason": None if ok else reason,
            }
        )
        all_ok = all_ok and ok
    return all_ok, trace


def _string_domains_conflict(a: list[Condition], b: list[Condition]) -> bool:
    """两组字符串条件的合取是否不可满足（按字段独立分析）。"""
    by_field: dict[str, list[Condition]] = {}
    for cond in a + b:
        if cond.field in STRING_FIELDS:
            by_field.setdefault(cond.field, []).append(cond)
    for conds in by_field.values():
        allowed: frozenset[str] | None = None
        forbidden: set[str] = set()
        for cond in conds:
            add_allowed, add_forbidden = cond.string_domain()
            if add_allowed is not None:
                allowed = add_allowed if allowed is None else allowed & add_allowed
            forbidden |= add_forbidden
        if allowed is not None and not (allowed - forbidden):
            return True
    return False


def _number_intervals_conflict(a: list[Condition], b: list[Condition]) -> bool:
    """两组数值条件的合取是否存在不可满足的字段。"""
    by_field: dict[str, list[Condition]] = {}
    for cond in a + b:
        if cond.field in NUMBER_FIELDS:
            by_field.setdefault(cond.field, []).append(cond)
    for field, conds in by_field.items():
        acc: tuple[float, float, bool, bool] | None = None
        for cond in conds:
            if cond.operator == "ne":
                continue  # 连续轴上剔除单点不会导致区间为空
            interval = cond.number_interval()
            acc = interval if acc is None else _intersect_intervals(acc, interval)
            if _interval_empty(acc):
                return True
        if acc is not None and not _interval_empty(acc):
            low, high, lo_inc, hi_inc = acc
            # eq 与 ne 的直接矛盾：交集只剩一个且被 ne 排除
            if low == high and lo_inc and hi_inc:
                ne_values = {c.value for c in conds if c.operator == "ne"}
                if low in ne_values:
                    return True
    return False


def are_compatible(
    conditions_a: list[dict[str, Any]] | list[Condition],
    conditions_b: list[dict[str, Any]] | list[Condition],
) -> bool:
    """两组条件是否 *可能同时命中同一车型*。

    返回 True 表示相容（存在车型同时满足两组条件）；
    返回 False 表示互斥，区间重叠时也不会产生解析歧义。
    不同字段之间默认相容。
    """
    a = _conditions_of(conditions_a)
    b = _conditions_of(conditions_b)
    if _string_domains_conflict(a, b):
        return False
    if _number_intervals_conflict(a, b):
        return False
    return True


def find_self_conflicts(
    conditions: list[dict[str, Any]] | list[Condition],
) -> list[dict[str, Any]]:
    """检测单个版本内部的自相矛盾条件。"""
    conds = _conditions_of(conditions)
    conflicts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def report(field: str, c1: Condition, c2: Condition, reason: str) -> None:
        key = (field, reason, repr(sorted([repr(c1.to_dict()), repr(c2.to_dict())])))
        if key not in seen:
            seen.add(key)
            conflicts.append(
                {
                    "field": field,
                    "reason": reason,
                    "conditions": [c1.to_dict(), c2.to_dict()],
                }
            )

    # 字符串字段
    by_field: dict[str, list[Condition]] = {}
    for cond in conds:
        by_field.setdefault(cond.field, []).append(cond)
    for field, group in by_field.items():
        if field in STRING_FIELDS:
            allowed: frozenset[str] | None = None
            forbidden: set[str] = set()
            for cond in group:
                add_allowed, add_forbidden = cond.string_domain()
                if add_allowed is not None:
                    new_allowed = (
                        add_allowed if allowed is None else allowed & add_allowed
                    )
                    if not (new_allowed - forbidden):
                        report(field, cond, cond, f"{field} 条件组合后无可能取值")
                        break
                    allowed = new_allowed
                if allowed is not None and add_forbidden & allowed:
                    report(field, cond, cond, f"{field} 禁止值清空了允许集合")
                    break
                forbidden |= add_forbidden
        else:
            acc: tuple[float, float, bool, bool] | None = None
            ne_values: set[float] = set()
            last_cond = group[0]
            for cond in group:
                last_cond = cond
                if cond.operator == "ne":
                    ne_values.add(cond.value)
                    continue
                interval = cond.number_interval()
                acc = interval if acc is None else _intersect_intervals(acc, interval)
                if _interval_empty(acc):
                    report(field, cond, cond, f"{field} 数值区间无交集")
                    break
            else:
                if acc is not None and not _interval_empty(acc):
                    low, high, lo_inc, hi_inc = acc
                    if low == high and lo_inc and hi_inc and low in ne_values:
                        report(field, group[0], last_cond, f"{field} 唯一取值又被排除")
    return conflicts
