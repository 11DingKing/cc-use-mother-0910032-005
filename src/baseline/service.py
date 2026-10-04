"""领域服务：规则版本生命周期与所有写操作。

写操作一律在 ``BEGIN IMMEDIATE`` 事务中“先重读、再校验、后写入”，
配合 SQLite 的库级写锁，保证并发发布被串行化，冲突检测不会漏判。
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from .conditions import Condition, are_compatible, find_self_conflicts
from .errors import (
    ForbiddenError,
    InvalidTransitionError,
    NotFoundError,
    RetroactiveDeniedError,
    RuleConflictError,
    SignatureRequiredError,
    ValidationError,
)
from .model import (
    ROLE_ACCOUNTANT,
    ROLE_AUDITOR,
    ROLE_ENTERPRISE_FILER,
    STATUS_CONFIRMED,
    STATUS_DRAFT,
    STATUS_PENDING,
    STATUS_SEALED,
    Actor,
    RuleVersion,
    intervals_overlap,
    parse_date,
)
from .repository import RuleRepository
from .resolver import resolve

# 发布前必须完成的签署角色
REQUIRED_SIGNATURE_ROLES = frozenset({ROLE_ACCOUNTANT, ROLE_AUDITOR})


def _new_id() -> str:
    return uuid.uuid4().hex


def _validate_payload(
    parameters: dict[str, Any],
    raw_conditions: list[dict[str, Any]],
    effective_start: str | date,
    effective_end: str | date | None,
) -> tuple[list[dict[str, Any]], date, date | None]:
    if not isinstance(parameters, dict) or not parameters:
        raise ValidationError("基线参数必须是非空对象")
    for key, value in parameters.items():
        if not isinstance(key, str) or not key:
            raise ValidationError("参数名必须是非空字符串")
        if not isinstance(value, (int, float, str)) or isinstance(value, bool):
            raise ValidationError(f"参数 {key} 必须是数值或字符串")

    if not isinstance(raw_conditions, list) or not raw_conditions:
        raise ValidationError("适用车型条件必须是非空列表")
    conditions = [Condition.from_dict(c).to_dict() for c in raw_conditions]
    self_conflicts = find_self_conflicts(conditions)
    if self_conflicts:
        raise ValidationError("版本内部条件自相矛盾", self_conflicts)

    start = parse_date(effective_start)
    end = parse_date(effective_end) if effective_end else None
    if end is not None and start >= end:
        raise ValidationError("生效起点必须早于失效点（区间为 [start, end)）")
    return conditions, start, end


def _coverage_end(v: RuleVersion) -> date | None:
    """版本对未来的实际覆盖终点：撤回会截断覆盖，但不影响撤回前历史。"""
    candidates = [v.effective_end]
    if v.withdrawn_at is not None:
        candidates.append(v.withdrawn_at.date())
    candidates = [c for c in candidates if c is not None]
    return min(candidates) if candidates else None


class RuleService:
    """规则库应用服务。"""

    def __init__(self, repository: RuleRepository, clock: Any = datetime.now) -> None:
        self.repo = repository
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock()

    # ---- 读 ----

    def list_rules(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.repo.list_rules()]

    def list_versions(self, rule_code: str | None = None) -> list[dict[str, Any]]:
        return [v.to_dict() for v in self.repo.list_versions(rule_code)]

    def get_version(self, version_id: str) -> dict[str, Any]:
        with self.repo.read_only() as conn:
            version = self.repo.get_version(conn, version_id)
        if version is None:
            raise NotFoundError(f"版本不存在：{version_id}")
        return version.to_dict()

    def history(self, version_id: str) -> list[dict[str, Any]]:
        self.get_version(version_id)  # 存在性检查
        return self.repo.list_events(version_id)

    def resolve(
        self, day: str | date, vehicle: dict[str, Any], rule_code: str | None = None
    ) -> dict[str, Any]:
        result = resolve(self.repo.list_versions(), day, vehicle, rule_code=rule_code)
        return result

    # ---- 草稿与发布生命周期 ----

    def create_draft(
        self,
        actor: Actor,
        rule_code: str,
        title: str,
        parameters: dict[str, Any],
        conditions: list[dict[str, Any]],
        effective_start: str | date,
        effective_end: str | date | None = None,
        *,
        supersedes_version: int | None = None,
    ) -> dict[str, Any]:
        if actor.role != ROLE_ENTERPRISE_FILER:
            raise ForbiddenError("仅企业申报员可以创建规则草稿")
        if not title or not isinstance(title, str):
            raise ValidationError("规则标题不能为空")
        conditions, start, end = _validate_payload(
            parameters, conditions, effective_start, effective_end
        )

        with self.repo.transaction() as conn:
            now = self._now()
            row = self.repo.get_rule(conn, rule_code)
            if row is None:
                self.repo.insert_rule(conn, rule_code, title, now)
                next_number = 1
            else:
                next_number = row["latest_version"] + 1
            if supersedes_version is not None:
                target = self.repo.get_version_by_number(
                    conn, rule_code, supersedes_version
                )
                if target is None:
                    raise ValidationError(
                        f"被替代版本 {rule_code} v{supersedes_version} 不存在"
                    )
                if target.status not in (STATUS_CONFIRMED, STATUS_SEALED):
                    raise ValidationError("只能替代已发布的版本")
            version = RuleVersion(
                rule_code=rule_code,
                version=next_number,
                title=title,
                parameters=parameters,
                conditions=conditions,
                effective_start=start,
                effective_end=end,
                supersedes_version=supersedes_version,
                status=STATUS_DRAFT,
                version_id=_new_id(),
                draft_owner=actor,
                created_at=now,
                updated_at=now,
            )
            self.repo.insert_version(conn, version)
            self.repo.bump_rule(conn, rule_code, next_number, now)
            self.repo.add_event(
                conn, version.version_id, "create_draft", actor, now,
                {"supersedes_version": supersedes_version},
            )
            return version.to_dict()

    def _load_in_tx(self, conn: Any, version_id: str) -> RuleVersion:
        version = self.repo.get_version(conn, version_id)
        if version is None:
            raise NotFoundError(f"版本不存在：{version_id}")
        return version

    def update_draft(
        self,
        actor: Actor,
        version_id: str,
        *,
        parameters: dict[str, Any] | None = None,
        conditions: list[dict[str, Any]] | None = None,
        effective_start: str | date | None = None,
        effective_end: str | date | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """仅草稿内容可改；已发布版本任何内容字段都不可改。"""
        if actor.role not in (ROLE_ENTERPRISE_FILER, ROLE_ACCOUNTANT):
            raise ForbiddenError("无权修改草稿")
        with self.repo.transaction() as conn:
            version = self._load_in_tx(conn, version_id)
            if version.status != STATUS_DRAFT:
                raise InvalidTransitionError(
                    f"版本状态为 {version.status}，内容已冻结，不能修改"
                )
            new_params = parameters if parameters is not None else version.parameters
            new_cond_raw = conditions if conditions is not None else version.conditions
            new_start = effective_start or version.effective_start
            new_end = effective_end if effective_end is not None else version.effective_end
            new_conditions, start, end = _validate_payload(
                new_params, new_cond_raw, new_start, new_end
            )
            now = self._now()
            version.parameters = new_params
            version.conditions = new_conditions
            version.effective_start = start
            version.effective_end = end
            if title:
                version.title = title
            version.updated_at = now
            self.repo.update_version(conn, version)
            self.repo.add_event(conn, version_id, "update_draft", actor, now)
            return version.to_dict()

    def submit(self, actor: Actor, version_id: str) -> dict[str, Any]:
        """草稿 → 待核算。"""
        if actor.role != ROLE_ENTERPRISE_FILER:
            raise ForbiddenError("仅企业申报员可以提交核算")
        with self.repo.transaction() as conn:
            version = self._load_in_tx(conn, version_id)
            self._transition(conn, version, STATUS_DRAFT, STATUS_PENDING, actor)
            return version.to_dict()

    def return_for_revision(self, actor: Actor, version_id: str, reason: str) -> dict[str, Any]:
        """待核算 → 草稿（核算专员退回）。"""
        if actor.role != ROLE_ACCOUNTANT:
            raise ForbiddenError("仅核算专员可以退回修订")
        if not reason:
            raise ValidationError("退回必须说明原因")
        with self.repo.transaction() as conn:
            version = self._load_in_tx(conn, version_id)
            self._transition(
                conn, version, STATUS_PENDING, STATUS_DRAFT, actor, {"reason": reason}
            )
            return version.to_dict()

    def _transition(
        self,
        conn: Any,
        version: RuleVersion,
        expected: str,
        target: str,
        actor: Actor,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if version.status != expected:
            raise InvalidTransitionError(
                f"版本当前状态为 {version.status}，不能迁移到 {target}"
            )
        now = self._now()
        version.status = target
        version.updated_at = now
        self.repo.update_version(conn, version)
        self.repo.add_event(
            conn, version.version_id,
            f"{expected}->{target}", actor, now, detail,
        )

    def sign(self, actor: Actor, version_id: str, note: str = "") -> dict[str, Any]:
        """在待核算版本上完成本角色签署；每角色至多一次。"""
        if actor.role not in REQUIRED_SIGNATURE_ROLES:
            raise ForbiddenError(f"角色 {actor.role} 不具备发布签署资格")
        with self.repo.transaction() as conn:
            version = self._load_in_tx(conn, version_id)
            if version.status != STATUS_PENDING:
                raise InvalidTransitionError(
                    f"仅待核算版本可以签署，当前状态 {version.status}"
                )
            if actor.role in version.signature_roles:
                raise ValidationError(f"角色 {actor.role} 已经签署过")
            now = self._now()
            version.signatures.append(
                {
                    "actor_id": actor.actor_id,
                    "name": actor.name,
                    "role": actor.role,
                    "note": note,
                    "signed_at": now.isoformat(),
                }
            )
            version.updated_at = now
            self.repo.update_version(conn, version)
            self.repo.add_event(conn, version_id, "sign", actor, now, {"note": note})
            return version.to_dict()

    def publish(self, actor: Actor, version_id: str) -> dict[str, Any]:
        """待核算 → 已确认（发布）。

        事务内重新加载全部已发布版本做冲突检测，保证并发场景下
        后发布者一定能看到先提交的版本。
        """
        if actor.role != ROLE_AUDITOR:
            raise ForbiddenError("仅监管审计员可以发布规则版本")
        with self.repo.transaction() as conn:
            version = self._load_in_tx(conn, version_id)
            if version.status != STATUS_PENDING:
                raise InvalidTransitionError(
                    f"仅待核算版本可以发布，当前状态 {version.status}"
                )

            missing = REQUIRED_SIGNATURE_ROLES - version.signature_roles
            if missing:
                raise SignatureRequiredError(
                    "发布前签署不完整", {"missing_roles": sorted(missing)}
                )

            today = self._now().date()
            if version.effective_start < today:
                raise RetroactiveDeniedError(
                    "生效起点早于发布当日：新规则不得追溯改变历史适用",
                    {
                        "effective_start": version.effective_start.isoformat(),
                        "publish_date": today.isoformat(),
                    },
                )

            self._detect_conflicts(conn, version)

            now = self._now()
            version.status = STATUS_CONFIRMED
            version.published_at = now
            version.updated_at = now
            self.repo.update_version(conn, version)
            self.repo.add_event(conn, version_id, "publish", actor, now)
            return version.to_dict()

    def _detect_conflicts(self, conn: Any, candidate: RuleVersion) -> None:
        """区间重叠 + 条件相容 即冲突；同一替代链上的成对版本除外。"""
        published = self.repo.list_published_candidates(conn)
        conflicts: list[dict[str, Any]] = []
        cand_end = candidate.effective_end
        for other in published:
            # 同一替代链：新版本声明替代该旧版本，属正常换代而非冲突
            if (
                other.rule_code == candidate.rule_code
                and candidate.supersedes_version == other.version
            ):
                continue
            other_end = _coverage_end(other)
            if not intervals_overlap(
                candidate.effective_start, cand_end,
                other.effective_start, other_end,
            ):
                continue
            if are_compatible(candidate.conditions, other.conditions):
                conflicts.append(
                    {
                        "reason": "生效区间重叠且车型条件可能同时命中",
                        "other_version_id": other.version_id,
                        "rule_code": other.rule_code,
                        "version": other.version,
                        "other_interval": [
                            other.effective_start.isoformat(),
                            other_end.isoformat() if other_end else None,
                        ],
                    }
                )
        if conflicts:
            raise RuleConflictError("发布前检测到区间重叠与条件冲突", conflicts)

    # ---- 撤回 / 紧急勘误 / 未来预告 / 封存 ----

    def withdraw(self, actor: Actor, version_id: str, reason: str) -> dict[str, Any]:
        """撤回已发布版本。

        撤回自“当前时刻”起生效，不修改生效区间、参数与签署；
        撤回日之前的历史核算仍解析到该版本。
        """
        if actor.role != ROLE_AUDITOR:
            raise ForbiddenError("仅监管审计员可以撤回规则版本")
        if not reason:
            raise ValidationError("撤回必须说明原因")
        with self.repo.transaction() as conn:
            version = self._load_in_tx(conn, version_id)
            if version.status != STATUS_CONFIRMED or version.withdrawn_at is not None:
                raise InvalidTransitionError("只能撤回已发布且未撤回的版本")
            now = self._now()
            version.withdrawn_at = now
            version.withdraw_reason = reason
            version.updated_at = now
            self.repo.update_version(conn, version)
            self.repo.add_event(
                conn, version_id, "withdraw", actor, now, {"reason": reason}
            )
            return version.to_dict()

    def emergency_correction(
        self,
        actor: Actor,
        version_id: str,
        parameters: dict[str, Any],
        *,
        effective_start: str | date | None = None,
        reason: str,
    ) -> dict[str, Any]:
        """对已发布版本发起紧急勘误：新建替代版本草稿。

        - 被勘误版本原样保留，其历史区间的核算结果不变；
        - 勘误版本生效起点不得早于当前日期（禁止追溯）；
        - 参数必须确实发生变化，避免无意义勘误。
        """
        if actor.role != ROLE_AUDITOR:
            raise ForbiddenError("仅监管审计员可以发起紧急勘误")
        if not reason:
            raise ValidationError("紧急勘误必须说明原因")
        with self.repo.transaction() as conn:
            target = self._load_in_tx(conn, version_id)
            if target.status not in (STATUS_CONFIRMED, STATUS_SEALED):
                raise InvalidTransitionError("只能勘误已发布版本")
            if parameters == target.parameters:
                raise ValidationError("勘误参数与原版本完全相同，没有需要纠正的内容")
            start = parse_date(effective_start) if effective_start else self._now().date()
            if start < self._now().date():
                raise RetroactiveDeniedError("勘误生效起点不能早于当前日期")
            # 复用 create 逻辑的校验，但直接在当前事务内构建
            conditions, start, end = _validate_payload(
                parameters, target.conditions, start, target.effective_end
            )
            now = self._now()
            row = self.repo.get_rule(conn, target.rule_code)
            next_number = row["latest_version"] + 1
            version = RuleVersion(
                rule_code=target.rule_code,
                version=next_number,
                title=target.title,
                parameters=parameters,
                conditions=conditions,
                effective_start=start,
                effective_end=end,
                supersedes_version=target.version,
                status=STATUS_DRAFT,
                version_id=_new_id(),
                draft_owner=actor,
                created_at=now,
                updated_at=now,
            )
            self.repo.insert_version(conn, version)
            self.repo.bump_rule(conn, target.rule_code, next_number, now)
            self.repo.add_event(
                conn, version.version_id, "emergency_correction", actor, now,
                {"corrects_version_id": target.version_id, "reason": reason},
            )
            self.repo.add_event(
                conn, target.version_id, "correction_raised", actor, now,
                {"new_version_id": version.version_id, "reason": reason},
            )
            return version.to_dict()

    def announce_future(
        self,
        actor: Actor,
        rule_code: str,
        title: str,
        parameters: dict[str, Any],
        conditions: list[dict[str, Any]],
        effective_start: str | date,
        effective_end: str | date | None = None,
        *,
        supersedes_version: int | None = None,
    ) -> dict[str, Any]:
        """登记未来版本预告（草稿），生效起点必须晚于今天。

        预告在发布且生效日到达前不参与任何日期的解析，
        因此完全不改变当下与历史的核算结果。
        """
        if actor.role != ROLE_ENTERPRISE_FILER:
            raise ForbiddenError("仅企业申报员可以登记未来版本预告")
        conditions, start, end = _validate_payload(
            parameters, conditions, effective_start, effective_end
        )
        if start <= self._now().date():
            raise ValidationError("未来版本预告的生效起点必须晚于当前日期")
        draft = self.create_draft(
            actor, rule_code, title, parameters, conditions, start, end,
            supersedes_version=supersedes_version,
        )
        with self.repo.transaction() as conn:
            self.repo.add_event(
                conn, draft["version_id"], "announce_future", actor, self._now(),
                {"effective_start": start.isoformat()},
            )
        return draft

    def seal(self, actor: Actor, version_id: str) -> dict[str, Any]:
        """已撤回版本封存归档（终态）。"""
        if actor.role != ROLE_AUDITOR:
            raise ForbiddenError("仅监管审计员可以封存版本")
        with self.repo.transaction() as conn:
            version = self._load_in_tx(conn, version_id)
            if version.status != STATUS_CONFIRMED or version.withdrawn_at is None:
                raise InvalidTransitionError("只能封存已经撤回的版本")
            now = self._now()
            version.status = STATUS_SEALED
            version.sealed_at = now
            version.updated_at = now
            self.repo.update_version(conn, version)
            self.repo.add_event(conn, version_id, "seal", actor, now)
            return version.to_dict()

    # ---- 核算快照（历史冻结的物证）----

    def record_accounting(
        self,
        actor: Actor,
        period: str,
        target_date: str | date,
        vehicle: dict[str, Any],
        accounting_id: str | None = None,
    ) -> dict[str, Any]:
        """按日期解析规则并把参数快照追加存档，后续任何规则变动不影响它。"""
        if actor.role not in (ROLE_ACCOUNTANT, ROLE_ENTERPRISE_FILER):
            raise ForbiddenError("仅核算专员或企业申报员可以登记核算")
        if not period:
            raise ValidationError("核算周期不能为空")
        with self.repo.transaction() as conn:
            result = resolve(
                self.repo.list_versions_in_tx(conn),
                target_date, vehicle,
            )
            if not result["matched"]:
                raise NotFoundError(
                    "当日不存在适用规则，无法核算", result["match_path"]
                )
            now = self._now()
            record = {
                "accounting_id": accounting_id or _new_id(),
                "period": period,
                "vehicle": vehicle,
                "target_date": parse_date(target_date),
                "rule_code": result["rule_code"],
                "version": result["version"],
                "version_id": result["version_id"],
                "parameters_snapshot": result["parameters"],
                "created_by": actor,
                "created_at": now,
            }
            try:
                self.repo.insert_accounting(conn, record)
            except Exception as exc:  # 主键重复等
                raise ValidationError("核算编号重复或数据不合法") from exc
            self.repo.add_event(
                conn, result["version_id"], "accounting_recorded", actor, now,
                {"accounting_id": record["accounting_id"], "period": period},
            )
            return {k: (v.isoformat() if isinstance(v, (date, datetime)) else v)
                    for k, v in record.items() if k != "created_by"} | {
                "created_by": actor.to_dict()}

    def list_accountings(self, period: str | None = None) -> list[dict[str, Any]]:
        return self.repo.list_accountings(period)
