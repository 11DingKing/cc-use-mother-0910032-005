"""按日期与车型解析唯一有效规则，并解释匹配路径。"""
from __future__ import annotations

from datetime import date
from typing import Any

from .conditions import condition_matches
from .errors import AmbiguousRuleError
from .model import RuleVersion, lifecycle_state, parse_date


def resolve(
    versions: list[RuleVersion],
    day: str | date,
    vehicle: dict[str, Any],
    *,
    rule_code: str | None = None,
) -> dict[str, Any]:
    """在给定版本集合中，解析 ``day`` 当日适用于 ``vehicle`` 的唯一版本。

    返回匹配结果与完整的匹配路径解释；当约束被破坏导致多版本
    同时命中时抛出 :class:`AmbiguousRuleError`，绝不静默二选一。
    """
    day = parse_date(day)
    candidates = [v for v in versions if rule_code is None or v.rule_code == rule_code]

    evaluated: list[dict[str, Any]] = []
    matched: list[RuleVersion] = []

    # 先计算“替代者在该日有效”的集合
    by_number = {(v.rule_code, v.version): v for v in candidates}
    active_supersessions: dict[str, str] = {}
    for v in candidates:
        if v.supersedes_version is not None and v.active_on(day):
            old = by_number.get((v.rule_code, v.supersedes_version))
            if old is not None:
                active_supersessions[old.version_id] = v.version_id

    for v in candidates:
        trace: dict[str, Any] = {
            "version_id": v.version_id,
            "rule_code": v.rule_code,
            "version": v.version,
            "status": lifecycle_state(v, day),
            "supersedes_version": v.supersedes_version,
        }
        if not v.active_on(day):
            if v.status not in ("已确认", "已封存"):
                trace["stage"] = "status_filter"
                trace["eliminated_reason"] = f"版本状态为 {v.status}，未发布"
            elif day < v.effective_start:
                trace["stage"] = "interval_filter"
                trace["eliminated_reason"] = (
                    f"{day.isoformat()} 早于生效日 {v.effective_start.isoformat()}"
                )
            elif v.effective_end is not None and day >= v.effective_end:
                trace["stage"] = "interval_filter"
                trace["eliminated_reason"] = (
                    f"{day.isoformat()} 已到达失效日 {v.effective_end.isoformat()}"
                )
            elif v.withdrawn_at is not None and day >= v.withdrawn_at.date():
                trace["stage"] = "withdraw_filter"
                trace["eliminated_reason"] = (
                    f"版本已于 {v.withdrawn_at.date().isoformat()} 撤回"
                    + ("（封存）" if v.status == "已封存" else "")
                )
            else:  # pragma: no cover - active_on 的分支已全部覆盖
                trace["stage"] = "interval_filter"
                trace["eliminated_reason"] = "当日不在适用区间"
            evaluated.append(trace)
            continue

        cond_ok, cond_trace = condition_matches(v.conditions, vehicle)
        trace["stage"] = "condition_match"
        trace["condition_trace"] = cond_trace
        if not cond_ok:
            trace["eliminated_reason"] = "车型不满足全部适用条件"
            evaluated.append(trace)
            continue

        if v.version_id in active_supersessions:
            replacer_id = active_supersessions[v.version_id]
            trace["stage"] = "supersede_shadow"
            trace["eliminated_reason"] = f"已被当日有效的版本 {replacer_id} 替代"
            trace["superseded_by"] = replacer_id
            evaluated.append(trace)
            continue

        trace["eliminated_reason"] = None
        evaluated.append(trace)
        matched.append(v)

    path: dict[str, Any] = {
        "target_date": day.isoformat(),
        "vehicle": vehicle,
        "filters": [
            "状态过滤（仅已发布）",
            "生效区间过滤 [effective_start, effective_end)，撤回日之后剔除",
            "车型条件合取匹配 (AND)",
            "替代关系遮蔽（被当日有效的新版本替代）",
        ],
        "evaluated": evaluated,
    }

    if not matched:
        return {
            "matched": False,
            "rule_code": rule_code,
            "version": None,
            "reason": "当日没有适用于该车型的有效版本",
            "match_path": path,
        }

    if len(matched) > 1:
        detail = [
            {"version_id": v.version_id, "rule_code": v.rule_code, "version": v.version}
            for v in matched
        ]
        raise AmbiguousRuleError(
            f"{day.isoformat()} 的车型同时命中 {len(matched)} 个有效版本", detail
        )

    winner = matched[0]
    return {
        "matched": True,
        "rule_code": winner.rule_code,
        "version": winner.version,
        "version_id": winner.version_id,
        "parameters": winner.parameters,
        "effective_start": winner.effective_start.isoformat(),
        "effective_end": winner.effective_end.isoformat()
        if winner.effective_end
        else None,
        "supersedes_version": winner.supersedes_version,
        "lifecycle_state": lifecycle_state(winner, day),
        "match_path": path,
    }
