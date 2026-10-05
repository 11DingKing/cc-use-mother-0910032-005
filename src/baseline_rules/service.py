"""基线规则库服务：发布门禁、撤回/勘误/预告与按日期解析。

线程安全约定：任何 "检查 -> 写入" 的组合都在同一把全局锁内完成，
因此并发发布只会有一个版本胜出；冲突版本在锁内被拒绝。
"""
from __future__ import annotations

import threading
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from .conflict import (
    explain_overlap,
    internally_consistent,
    jointly_satisfiable,
    same_coverage,
)
from .errors import (
    AmbiguousRuleError,
    ConflictError,
    InvalidRuleContent,
    NoApplicableRuleError,
    NotFoundError,
)
from .models import Criterion, Draft, Event, Resolution, RuleVersion, Withdrawal


def _parse_day(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise InvalidRuleContent(f"日期格式非法：{value!r}，应为 YYYY-MM-DD") from None


class BaselineRuleService:
    """内存型规则库。

    所有已发布版本与签署记录只追加、不修改；可通过 :meth:`snapshot`
    与 :meth:`restore_snapshot` 做持久化归档。
    """

    def __init__(self, today: date | None = None) -> None:
        self._lock = threading.RLock()
        self._today_override = today
        self._versions: dict[str, RuleVersion] = {}
        self._rule_versions: dict[str, list[str]] = {}
        self._rule_counter: dict[str, int] = {}
        self._withdrawals: dict[str, Withdrawal] = {}
        self._events: list[Event] = []

    # ----- 基础查询 -------------------------------------------------------

    @property
    def today(self) -> date:
        return self._today_override or date.today()

    def set_today(self, day: str | date) -> None:
        """推进应用时钟（不允许回拨，避免借时钟绕过追溯禁令）。"""
        new_day = _parse_day(day)
        if self._today_override is not None and new_day < self._today_override:
            raise InvalidRuleContent("应用时钟只能向前推进")
        self._today_override = new_day

    def _require_version(self, version_id: str) -> RuleVersion:
        try:
            return self._versions[version_id]
        except KeyError:
            raise NotFoundError(f"规则版本不存在：{version_id}") from None

    def get_version(self, version_id: str) -> RuleVersion:
        with self._lock:
            return self._require_version(version_id)

    def list_versions(self, rule_id: str) -> list[RuleVersion]:
        with self._lock:
            return [self._versions[v] for v in self._rule_versions.get(rule_id, [])]

    def list_all_versions(self) -> list[RuleVersion]:
        with self._lock:
            return list(self._versions.values())

    def get_withdrawal(self, version_id: str) -> Withdrawal | None:
        with self._lock:
            return self._withdrawals.get(version_id)

    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    # ----- 草稿构造 -------------------------------------------------------

    @staticmethod
    def build_criteria(raw: Sequence[Mapping[str, Any] | Criterion]) -> tuple[Criterion, ...]:
        if not raw:
            raise InvalidRuleContent("规则至少需要一条适用车型条件")
        criteria = tuple(c if isinstance(c, Criterion) else Criterion.from_dict(c) for c in raw)
        contradiction = internally_consistent(criteria)
        if contradiction:
            raise InvalidRuleContent(contradiction)
        return criteria

    def draft(
        self,
        rule_id: str,
        effective_from: str | date,
        criteria: Sequence[Mapping[str, Any] | Criterion],
        parameters: Mapping[str, Any],
        effective_to: str | date | None = None,
        kind: str = "regular",
        supersedes: str | None = None,
        basis: str | None = None,
    ) -> Draft:
        try:
            parsed_from = _parse_day(effective_from)
            parsed_to = _parse_day(effective_to) if effective_to else None
            parsed_criteria = self.build_criteria(criteria)
            params = dict(parameters)
            if not params:
                raise InvalidRuleContent("基线参数不能为空")
            if kind not in {"regular", "errata", "preview"}:
                raise InvalidRuleContent(f"未知发布类型：{kind}")
            if parsed_to is not None and parsed_to < parsed_from:
                raise InvalidRuleContent("生效截止日不能早于生效起始日")
            return Draft(
                rule_id=rule_id,
                effective_from=parsed_from,
                effective_to=parsed_to,
                criteria=parsed_criteria,
                parameters=params,
                kind=kind,
                supersedes=supersedes,
                basis=basis,
            )
        except (ValueError, TypeError) as exc:
            if isinstance(exc, InvalidRuleContent):
                raise
            raise InvalidRuleContent(str(exc)) from None

    # ----- 发布门禁 -------------------------------------------------------

    def publish(self, draft: Draft, signer: str) -> RuleVersion:
        """校验并发布草稿。整个 check-then-insert 在锁内原子完成。"""
        if not signer or not signer.strip():
            raise InvalidRuleContent("发布必须记录签署人")
        with self._lock:
            self._validate_temporal_rules(draft)
            self._validate_supersedes(draft)
            conflicts, coexisting = self._detect_conflicts(draft)
            if conflicts:
                raise ConflictError(
                    f"草稿 {draft.rule_id} 与 {len(conflicts)} 个已发布版本冲突，发布被拒绝",
                    conflicts,
                )
            version = self._insert_version(draft, signer)
            self._append_event(
                signer,
                f"publish:{draft.kind}",
                version.version_id,
                {
                    "effective_from": version.effective_from.isoformat(),
                    "effective_to": version.effective_to.isoformat() if version.effective_to else None,
                    "supersedes": version.supersedes,
                    "basis": version.basis,
                    "coexisting": [c["other"] for c in coexisting],
                },
            )
            return version

    def _validate_temporal_rules(self, draft: Draft) -> None:
        """禁止追溯：任何新版本都不得早于签署日起生效。"""
        if draft.kind == "preview":
            if draft.effective_from <= self.today:
                raise InvalidRuleContent("预告版本的生效起始日必须晚于发布日")
        elif draft.effective_from < self.today:
            raise InvalidRuleContent(
                f"生效起始日 {draft.effective_from} 早于发布日 {self.today}，禁止追溯生效"
            )
        if draft.kind == "errata":
            if not draft.supersedes:
                raise InvalidRuleContent("紧急勘误必须通过 supersedes 指明被勘误版本")
            if not draft.basis or not draft.basis.strip():
                raise InvalidRuleContent("紧急勘误必须填写勘误原因 basis")

    def _validate_supersedes(self, draft: Draft) -> RuleVersion | None:
        if not draft.supersedes:
            return None
        target = self._versions.get(draft.supersedes)
        if target is None:
            raise InvalidRuleContent(f"被替代版本不存在：{draft.supersedes}")
        if target.rule_id != draft.rule_id:
            raise InvalidRuleContent(
                f"替代关系必须位于同一规则族内：{draft.supersedes} 属于 {target.rule_id}"
            )
        if target.kind == "preview" and draft.kind != "regular":
            raise InvalidRuleContent("预告版本只能由同族的正式发布承接（supersedes）")
        if draft.effective_from < target.effective_from:
            raise InvalidRuleContent("替代版本不得早于被替代版本的原始生效日，避免改写历史")
        equal_coverage, why = same_coverage(draft.criteria, target.criteria)
        if not equal_coverage:
            raise InvalidRuleContent(
                "替代版本的适用车型条件必须与被替代版本覆盖同一批车型：" + (why or "覆盖不一致")
            )
        return target

    def precheck(self, draft: Draft) -> dict[str, list[dict[str, Any]]]:
        """只做发布前检测不写入，返回冲突清单与可共存说明。"""
        with self._lock:
            self._validate_temporal_rules(draft)
            self._validate_supersedes(draft)
            conflicts, coexisting = self._detect_conflicts(draft)
            return {"conflicts": conflicts, "coexisting": coexisting}

    def _boundaries_in(self, lo: date, hi: date) -> list[date]:
        """草稿区间内所有效力切换点（各版本窗口起点、终点次日、撤回生效日）。

        ``hi`` 为闭区间右端；返回值额外包含次日哨兵，调用方据此切段。
        """
        sentinel = date.max if hi == date.max else hi + timedelta(days=1)
        points = {lo, sentinel}
        for v in self._versions.values():
            if lo <= v.effective_from <= hi:
                points.add(v.effective_from)
            if v.effective_to is not None and v.effective_to < hi:
                points.add(v.effective_to + timedelta(days=1))
        for w in self._withdrawals.values():
            if lo <= w.effective_from <= hi:
                points.add(w.effective_from)
        return sorted(points)

    def _governs_at(self, v: RuleVersion, day: date) -> bool:
        """v 在该日是否为有效治理版本。

        口径与 :meth:`resolve` 完全一致：窗口覆盖、未撤回、未被生效中的
        同族替代版本遮蔽。预告版本也算占位（它会在未来真正参与解析，
        因此同样要阻止与之冲突的发布）。
        """
        if not v.window_covers(day):
            return False
        w = self._withdrawals.get(v.version_id)
        if w is not None and day >= w.effective_from:
            return False
        for other in self._versions.values():
            if other.supersedes != v.version_id or other.kind == "preview":
                continue
            if not other.window_covers(day):
                continue
            ow = self._withdrawals.get(other.version_id)
            if ow is not None and day >= ow.effective_from:
                continue
            return False
        return True

    def _detect_conflicts(
        self, draft: Draft
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """按效力切换点把草稿区间切段，逐段比对当日的治理版本集合。

        只有 "区间重叠 且 条件可被同一车型同时满足" 才构成冲突（否则解析
        将不唯一）；条件互斥的版本（如 bev 与 hev 分治）允许同期共存。
        """
        conflicts: list[dict[str, Any]] = []
        coexisting: list[dict[str, Any]] = []
        hi = draft.effective_to or date.max
        boundaries = self._boundaries_in(draft.effective_from, hi)
        # 相邻切换点之间治理集合恒定；开放区间的最后一段代表其后的所有日期。
        representative_days = boundaries[:-1]
        seen_conflict: set[str] = set()
        seen_coexist: set[str] = set()
        for seg_start in representative_days:
            for v in self._versions.values():
                if draft.supersedes == v.version_id:
                    continue  # 显式替代/承接：重叠是预期的
                if not self._governs_at(v, seg_start):
                    continue
                compatible, reason = jointly_satisfiable(draft.criteria, v.criteria)
                if compatible and v.version_id not in seen_conflict:
                    seen_conflict.add(v.version_id)
                    info = explain_overlap(self._draft_as_view(draft), v)
                    info["reason"] = "生效区间重叠且适用条件可被同一车型同时满足，解析将不唯一"
                    conflicts.append(info)
                elif not compatible and v.version_id not in seen_coexist:
                    seen_coexist.add(v.version_id)
                    coexisting.append({
                        "other": v.version_id,
                        "reason": f"区间重叠但条件互斥（{reason}），允许同期分治",
                    })
        return conflicts, coexisting

    def _draft_as_view(self, draft: Draft) -> RuleVersion:
        """仅用于重叠说明的临时视图，不写入存储。"""
        return RuleVersion(
            rule_id=draft.rule_id,
            version=0,
            effective_from=draft.effective_from,
            effective_to=draft.effective_to,
            criteria=draft.criteria,
            parameters=draft.parameters,
            published_at=self.today,
            signer="(draft)",
        )

    def _insert_version(self, draft: Draft, signer: str) -> RuleVersion:
        number = self._rule_counter.get(draft.rule_id, 0) + 1
        self._rule_counter[draft.rule_id] = number
        version = RuleVersion(
            rule_id=draft.rule_id,
            version=number,
            effective_from=draft.effective_from,
            effective_to=draft.effective_to,
            criteria=draft.criteria,
            parameters=draft.parameters,
            published_at=self.today,
            signer=signer.strip(),
            kind=draft.kind,
            supersedes=draft.supersedes,
            basis=draft.basis,
        )
        if version.version_id in self._versions:  # pragma: no cover - 编号单调不可能冲突
            raise InvalidRuleContent(f"版本编号冲突：{version.version_id}")
        self._versions[version.version_id] = version
        self._rule_versions.setdefault(draft.rule_id, []).append(version.version_id)
        return version

    # ----- 撤回 / 勘易 / 预告（便捷入口） ----------------------------------

    def withdraw(
        self,
        version_id: str,
        signer: str,
        reason: str,
        effective_from: str | date | None = None,
    ) -> Withdrawal:
        """撤回签署：只截断版本未来的效力，历史日期仍解析到该版本。"""
        if not reason or not reason.strip():
            raise InvalidRuleContent("撤回必须填写原因")
        if not signer or not signer.strip():
            raise InvalidRuleContent("撤回必须记录签署人")
        day = _parse_day(effective_from) if effective_from else self.today
        if day < self.today:
            raise InvalidRuleContent("撤回不得追溯生效")
        with self._lock:
            version = self._require_version(version_id)
            if version_id in self._withdrawals:
                raise InvalidRuleContent(f"版本已撤回：{version_id}")
            record = Withdrawal(
                version_id=version_id,
                withdrawn_at=self.today,
                effective_from=day,
                signer=signer.strip(),
                reason=reason.strip(),
            )
            self._withdrawals[version_id] = record
            self._append_event(signer, "withdraw", version_id, {
                "effective_from": day.isoformat(),
                "reason": record.reason,
            })
            return record

    def errata(
        self,
        supersedes_version_id: str,
        criteria: Sequence[Mapping[str, Any] | Criterion],
        parameters: Mapping[str, Any],
        signer: str,
        basis: str,
        effective_from: str | date | None = None,
        effective_to: str | date | None = None,
    ) -> RuleVersion:
        """紧急勘误：生成新版本替代旧版，自勘误签署日起向前生效，不回溯。"""
        target = self.get_version(supersedes_version_id)
        draft = self.draft(
            rule_id=target.rule_id,
            effective_from=effective_from or self.today,
            effective_to=effective_to,
            criteria=criteria,
            parameters=parameters,
            kind="errata",
            supersedes=supersedes_version_id,
            basis=basis,
        )
        return self.publish(draft, signer)

    def preview(
        self,
        rule_id: str,
        effective_from: str | date,
        criteria: Sequence[Mapping[str, Any] | Criterion],
        parameters: Mapping[str, Any],
        signer: str,
        basis: str,
        effective_to: str | date | None = None,
    ) -> RuleVersion:
        """未来版本预告：占用未来区间参与冲突检测，但当前不参与适用解析。"""
        draft = self.draft(
            rule_id=rule_id,
            effective_from=effective_from,
            effective_to=effective_to,
            criteria=criteria,
            parameters=parameters,
            kind="preview",
            basis=basis,
        )
        return self.publish(draft, signer)

    # ----- 按日期 + 车型解析 ----------------------------------------------

    def resolve(self, day: str | date, vehicle: Mapping[str, Any]) -> Resolution:
        day = _parse_day(day)
        with self._lock:
            trace: list[dict[str, Any]] = []
            survivors: list[RuleVersion] = []
            ordered = sorted(self._versions.values(), key=lambda v: (v.published_at, v.version_id))
            for v in ordered:
                entry = self._evaluate_candidate(v, day, vehicle)
                trace.append(entry)
                if entry["eliminated"] is False:
                    survivors.append(v)
            status: str
            chosen: RuleVersion | None = None
            ambiguous: tuple[str, ...] = ()
            if len(survivors) == 1:
                status = "unique"
                chosen = survivors[0]
            elif not survivors:
                status = "none"
            else:
                status = "ambiguous"
                ambiguous = tuple(v.version_id for v in survivors)
            return Resolution(
                date=day,
                vehicle=dict(vehicle),
                trace=tuple(trace),
                considered=len(ordered),
                status=status,
                version=chosen,
                ambiguous_candidates=ambiguous,
            )

    def resolve_required(self, day: str | date, vehicle: Mapping[str, Any]) -> RuleVersion:
        result = self.resolve(day, vehicle)
        if result.status == "unique":
            return result.version  # type: ignore[return-value]
        if result.status == "none":
            raise NoApplicableRuleError(f"{day} 没有适用于该车型的基线规则")
        raise AmbiguousRuleError(
            f"{day} 该车型同时匹配多个有效版本：{', '.join(result.ambiguous_candidates)}"
        )

    def _evaluate_candidate(
        self, v: RuleVersion, day: date, vehicle: Mapping[str, Any]
    ) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []

        ok = v.window_covers(day)
        steps.append({
            "check": "生效窗口",
            "passed": ok,
            "detail": (
                f"目标日期 {day.isoformat()} 落在 "
                f"{v.effective_from.isoformat()} ~ "
                f"{v.effective_to.isoformat() if v.effective_to else '开放'}"
            ),
        })
        if not ok:
            return self._trace_entry(v, steps, "生效窗口不覆盖目标日期")

        withdrawal = self._withdrawals.get(v.version_id)
        if withdrawal is not None and day >= withdrawal.effective_from:
            steps.append({
                "check": "撤回状态",
                "passed": False,
                "detail": (
                    f"已由 {withdrawal.signer} 于 {withdrawal.withdrawn_at.isoformat()} "
                    f"签署撤回，自 {withdrawal.effective_from.isoformat()} 起失效"
                ),
            })
            return self._trace_entry(v, steps, "版本在目标日期已被撤回")

        if v.kind == "preview":
            steps.append({
                "check": "发布类型",
                "passed": False,
                "detail": "未来版本预告，正式发布前不参与适用解析",
            })
            return self._trace_entry(v, steps, "预告版本不参与解析")

        criterion_results = []
        matched = True
        for c in v.criteria:
            hit = c.matches(vehicle)
            matched = matched and hit
            criterion_results.append({
                "criterion": c.describe(),
                "passed": hit,
                "actual": vehicle.get(c.field),
            })
        steps.append({"check": "适用车型条件", "passed": matched, "criteria": criterion_results})
        if not matched:
            return self._trace_entry(v, steps, "车型不满足全部适用条件")

        successor = self._active_successor(v, day)
        if successor is not None:
            steps.append({
                "check": "替代关系",
                "passed": False,
                "detail": f"在目标日期已被 {successor.version_id}（{successor.kind}）替代遮蔽",
                "superseded_by": successor.version_id,
            })
            return self._trace_entry(v, steps, f"已被 {successor.version_id} 替代")
        steps.append({"check": "替代关系", "passed": True, "detail": "无生效中的替代版本"})

        return self._trace_entry(v, steps, None)

    def _active_successor(self, v: RuleVersion, day: date) -> RuleVersion | None:
        """目标日期对 v 生效的替代版本（沿替代链取最新治理者）。"""
        current = v
        seen: set[str] = set()
        while current.version_id not in seen:
            seen.add(current.version_id)
            nxt: RuleVersion | None = None
            for other in self._versions.values():
                if other.supersedes != current.version_id:
                    continue
                if other.kind == "preview":
                    continue
                if not other.window_covers(day):
                    continue
                w = self._withdrawals.get(other.version_id)
                if w is not None and day >= w.effective_from:
                    continue
                nxt = other
                break
            if nxt is None:
                return None if current is v else current
            current = nxt
        return current  # pragma: no cover - 替代链成环时防御

    @staticmethod
    def _trace_entry(
        v: RuleVersion, steps: list[dict[str, Any]], reason: str | None
    ) -> dict[str, Any]:
        return {
            "version_id": v.version_id,
            "rule_id": v.rule_id,
            "version": v.version,
            "kind": v.kind,
            "signature": v.signature,
            "steps": steps,
            "eliminated": reason is not None,
            "elimination_reason": reason,
        }

    # ----- 事件与归档 -----------------------------------------------------

    def _append_event(
        self, actor: str, action: str, target: str, detail: Mapping[str, Any]
    ) -> None:
        self._events.append(Event(
            seq=len(self._events) + 1,
            at=self.today,
            actor=actor.strip(),
            action=action,
            target=target,
            detail=dict(detail),
        ))

    def snapshot(self) -> dict[str, Any]:
        """生成可归档/可重放的完整快照（只含不可变记录）。"""
        with self._lock:
            return {
                "as_of": self.today.isoformat(),
                "versions": [v.to_dict() for v in self._versions.values()],
                "withdrawals": [w.to_dict() for w in self._withdrawals.values()],
                "events": [e.to_dict() for e in self._events],
            }

    @classmethod
    def restore_snapshot(
        cls, data: Mapping[str, Any], today: date | None = None
    ) -> "BaselineRuleService":
        """从快照重建规则库（用于审计重放与离线归档）。"""
        svc = cls(today=today)
        for raw in data.get("versions", []):
            version = RuleVersion(
                rule_id=raw["rule_id"],
                version=raw["version"],
                effective_from=date.fromisoformat(raw["effective_from"]),
                effective_to=(
                    date.fromisoformat(raw["effective_to"]) if raw.get("effective_to") else None
                ),
                criteria=tuple(Criterion.from_dict(c) for c in raw["criteria"]),
                parameters=dict(raw["parameters"]),
                published_at=date.fromisoformat(raw["published_at"]),
                signer=raw["signer"],
                kind=raw["kind"],
                supersedes=raw.get("supersedes"),
                basis=raw.get("basis"),
            )
            if version.signature != raw["signature"]:
                raise InvalidRuleContent(
                    f"快照中 {version.version_id} 的内容签名与归档不一致，拒绝载入"
                )
            svc._versions[version.version_id] = version
            svc._rule_versions.setdefault(version.rule_id, []).append(version.version_id)
            svc._rule_counter[version.rule_id] = max(
                svc._rule_counter.get(version.rule_id, 0), version.version
            )
        for raw in data.get("withdrawals", []):
            svc._withdrawals[raw["version_id"]] = Withdrawal(
                version_id=raw["version_id"],
                withdrawn_at=date.fromisoformat(raw["withdrawn_at"]),
                effective_from=date.fromisoformat(raw["effective_from"]),
                signer=raw["signer"],
                reason=raw["reason"],
            )
        for raw in data.get("events", []):
            svc._events.append(Event(
                seq=raw["seq"],
                at=date.fromisoformat(raw["at"]),
                actor=raw["actor"],
                action=raw["action"],
                target=raw["target"],
                detail=dict(raw.get("detail", {})),
            ))
        return svc
