"""规则库领域测试：生命周期、冲突检测、历史冻结、勘误、预告、并发发布。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from threading import Barrier, Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from baseline import (  # noqa: E402
    AmbiguousRuleError,
    Actor,
    RuleService,
    RetroactiveDeniedError,
    RuleConflictError,
    SignatureRequiredError,
    ValidationError,
    are_compatible,
    condition_matches,
    find_self_conflicts,
    resolve,
)
from baseline.repository import RuleRepository  # noqa: E402

FILER = Actor("e1", "王申报", "企业申报员")
ACCOUNTANT = Actor("a1", "李核算", "核算专员")
OPERATOR = Actor("o1", "张运营", "交易运营员")
AUDITOR = Actor("u1", "赵审计", "监管审计员")

BEV = {"energy_type": "bev", "curb_weight_kg": 1600, "vehicle_category": "乘用车"}
PHEV = {"energy_type": "phev", "curb_weight_kg": 1800, "vehicle_category": "乘用车"}


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FixedClock(datetime(2025, 12, 1, 9, 0, 0))
        self.service = RuleService(
            RuleRepository(Path(self.tmp.name) / "rules.db"), clock=self.clock
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def publish(
        self,
        rule_code: str = "BL-BEV",
        parameters=None,
        conditions=None,
        start="2026-01-01",
        end=None,
        supersedes=None,
    ) -> dict:
        """走完整签署发布流程，返回已发布版本。"""
        parameters = parameters or {"baseline_kwh_per_100km": 12.5}
        conditions = conditions or [{"field": "energy_type", "operator": "eq", "value": "bev"}]
        draft = self.service.create_draft(
            FILER, rule_code, "低能耗基线", parameters, conditions, start, end,
            supersedes_version=supersedes,
        )
        self.service.submit(FILER, draft["version_id"])
        self.service.sign(ACCOUNTANT, draft["version_id"], "核算复核通过")
        self.service.sign(AUDITOR, draft["version_id"], "合规通过")
        return self.service.publish(AUDITOR, draft["version_id"])


class ConditionEngineTest(unittest.TestCase):
    def test_string_and_number_operators(self) -> None:
        conds = [
            {"field": "energy_type", "operator": "in", "value": ["bev", "phev"]},
            {"field": "curb_weight_kg", "operator": "between", "value": [1000, 2000]},
        ]
        ok, trace = condition_matches(conds, BEV)
        self.assertTrue(ok)
        self.assertEqual(len(trace), 2)
        ok, trace = condition_matches(conds, {"energy_type": "ice", "curb_weight_kg": 1500})
        self.assertFalse(ok)
        self.assertIn("energy_type", trace[0]["reason"])

    def test_missing_vehicle_attribute_fails(self) -> None:
        ok, trace = condition_matches(
            [{"field": "range_km", "operator": "ge", "value": 400}], BEV
        )
        self.assertFalse(ok)
        self.assertIn("缺少属性", trace[0]["reason"])

    def test_self_conflict_string(self) -> None:
        conds = [
            {"field": "energy_type", "operator": "eq", "value": "bev"},
            {"field": "energy_type", "operator": "eq", "value": "phev"},
        ]
        self.assertTrue(find_self_conflicts(conds))

    def test_self_conflict_number(self) -> None:
        conds = [
            {"field": "curb_weight_kg", "operator": "ge", "value": 2000},
            {"field": "curb_weight_kg", "operator": "lt", "value": 1500},
        ]
        self.assertTrue(find_self_conflicts(conds))
        conds2 = [
            {"field": "curb_weight_kg", "operator": "eq", "value": 1500},
            {"field": "curb_weight_kg", "operator": "ne", "value": 1500},
        ]
        self.assertTrue(find_self_conflicts(conds2))

    def test_compatibility_matrix(self) -> None:
        bev = [{"field": "energy_type", "operator": "eq", "value": "bev"}]
        phev = [{"field": "energy_type", "operator": "eq", "value": "phev"}]
        self.assertFalse(are_compatible(bev, phev))  # 互斥
        light = bev + [{"field": "curb_weight_kg", "operator": "lt", "value": 1800}]
        heavy = bev + [{"field": "curb_weight_kg", "operator": "ge", "value": 1800}]
        self.assertFalse(are_compatible(light, heavy))
        # 不同字段之间相容
        self.assertTrue(are_compatible(
            bev, [{"field": "vehicle_category", "operator": "eq", "value": "乘用车"}]
        ))

    def test_invalid_condition_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self_published = None
            find_self_conflicts([{"field": "nope", "operator": "eq", "value": 1}])
        with self.assertRaises(ValidationError):
            condition_matches([{"field": "range_km", "operator": "??", "value": 1}], {})


class LifecycleTest(ServiceTestBase):
    def test_full_publish_and_resolve(self) -> None:
        v1 = self.publish()
        result = self.service.resolve(date(2026, 6, 1), BEV)
        self.assertTrue(result["matched"])
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["parameters"]["baseline_kwh_per_100km"], 12.5)
        self.assertEqual(result["lifecycle_state"], "执行中")
        # phev 不命中
        result = self.service.resolve(date(2026, 6, 1), PHEV)
        self.assertFalse(result["matched"])
        # 生效前不命中
        self.assertFalse(self.service.resolve(date(2025, 12, 31), BEV)["matched"])
        # 匹配路径可解释
        path = self.service.resolve(date(2026, 6, 1), BEV)["match_path"]
        self.assertTrue(any(
            step.get("stage") == "condition_match" and step.get("eliminated_reason") is None
            for step in path["evaluated"]
        ))
        events = self.service.history(v1["version_id"])
        self.assertIn("publish", [e["event"] for e in events])

    def test_roles_and_transitions(self) -> None:
        draft = self.service.create_draft(
            FILER, "BL", "t", {"x": 1},
            [{"field": "energy_type", "operator": "eq", "value": "bev"}],
            "2026-04-01",
        )
        with self.assertRaises(Exception):
            self.service.submit(AUDITOR, draft["version_id"])  # 角色错误
        self.service.submit(FILER, draft["version_id"])
        # 未签署完整不能发布
        with self.assertRaises(SignatureRequiredError) as ctx:
            self.service.publish(AUDITOR, draft["version_id"])
        self.assertEqual(set(ctx.exception.details["missing_roles"]),
                         {"核算专员", "监管审计员"})
        self.service.sign(ACCOUNTANT, draft["version_id"])
        with self.assertRaises(SignatureRequiredError):
            self.service.publish(AUDITOR, draft["version_id"])
        # 退回修订后签署清空需要重做
        self.service.return_for_revision(ACCOUNTANT, draft["version_id"], "参数存疑")
        self.assertEqual(self.service.get_version(draft["version_id"])["status"], "草稿")

    def test_retroactive_start_rejected(self) -> None:
        self.clock.now = datetime(2026, 3, 1, 9, 0)
        draft = self.service.create_draft(
            FILER, "BL", "t", {"x": 1},
            [{"field": "energy_type", "operator": "eq", "value": "bev"}],
            "2026-02-01",  # 早于当前 2026-03-01
        )
        self.service.submit(FILER, draft["version_id"])
        self.service.sign(ACCOUNTANT, draft["version_id"])
        self.service.sign(AUDITOR, draft["version_id"])
        with self.assertRaises(RetroactiveDeniedError):
            self.service.publish(AUDITOR, draft["version_id"])


class ConflictDetectionTest(ServiceTestBase):
    BEV_COND = [{"field": "energy_type", "operator": "eq", "value": "bev"}]

    def _draft_signed(self, start, end=None, conditions=None):
        draft = self.service.create_draft(
            FILER, "BL-BEV", "低能耗基线", {"v": 2.0},
            conditions or self.BEV_COND, start, end,
        )
        self.service.submit(FILER, draft["version_id"])
        self.service.sign(ACCOUNTANT, draft["version_id"])
        self.service.sign(AUDITOR, draft["version_id"])
        return draft

    def test_overlapping_interval_with_compatible_conditions_rejected(self) -> None:
        self.publish(start="2026-01-01", end="2026-12-31")
        draft = self._draft_signed("2026-06-01")  # 与 v1 区间重叠、条件相同
        with self.assertRaises(RuleConflictError) as ctx:
            self.service.publish(AUDITOR, draft["version_id"])
        self.assertEqual(len(ctx.exception.details), 1)
        self.assertIn("区间重叠", ctx.exception.details[0]["reason"])

    def test_overlap_with_mutually_exclusive_conditions_allowed(self) -> None:
        self.publish(
            start="2026-01-01",
            conditions=[{"field": "energy_type", "operator": "eq", "value": "bev"}],
        )
        draft = self._draft_signed(
            "2026-06-01",
            conditions=[{"field": "energy_type", "operator": "eq", "value": "phev"}],
        )
        published = self.service.publish(AUDITOR, draft["version_id"])
        self.assertEqual(published["status"], "已确认")
        # 同日不同车型解析各自唯一规则
        self.assertEqual(self.service.resolve("2026-07-01", BEV)["version"], 1)
        self.assertEqual(self.service.resolve("2026-07-01", PHEV)["version"], 2)

    def test_non_overlapping_intervals_allowed(self) -> None:
        self.publish(start="2026-01-01", end="2026-06-01")
        draft = self._draft_signed("2026-06-01")  # 半开区间，恰好衔接不重叠
        published = self.service.publish(AUDITOR, draft["version_id"])
        self.assertEqual(published["version"], 2)

    def test_supersession_chain_not_conflict(self) -> None:
        self.publish(start="2026-01-01", parameters={"v": 1.0})
        # 新版本明确替代 v1，区间重叠是合法换代
        v2 = self.service.create_draft(
            FILER, "BL-BEV", "低能耗基线", {"v": 2.0}, self.BEV_COND,
            "2026-06-01", supersedes_version=1,
        )
        self.service.submit(FILER, v2["version_id"])
        self.service.sign(ACCOUNTANT, v2["version_id"])
        self.service.sign(AUDITOR, v2["version_id"])
        published = self.service.publish(AUDITOR, v2["version_id"])
        self.assertEqual(published["supersedes_version"], 1)
        # 6 月前解析 v1，6 月起解析 v2（替代遮蔽）
        self.assertEqual(self.service.resolve("2026-05-31", BEV)["version"], 1)
        result = self.service.resolve("2026-06-01", BEV)
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["supersedes_version"], 1)


class HistoryFreezeTest(ServiceTestBase):
    def test_withdraw_does_not_rewrite_history(self) -> None:
        v1 = self.publish(start="2026-01-01", parameters={"v": 1.0})
        # 完成一次年度申报核算并快照
        acc = self.service.record_accounting(
            ACCOUNTANT, "2025年度", date(2026, 2, 1), BEV, accounting_id="ACC-1"
        )
        self.assertEqual(acc["parameters_snapshot"], {"v": 1.0})

        # 3 月 15 日撤回
        self.clock.now = datetime(2026, 3, 15, 10, 0)
        self.service.withdraw(AUDITOR, v1["version_id"], "口径错误，停用待勘误")

        # 历史日期仍解析到撤回版本（执行中视图），参数不变
        hist = self.service.resolve(date(2026, 2, 1), BEV)
        self.assertTrue(hist["matched"])
        self.assertEqual(hist["version"], 1)
        self.assertEqual(hist["parameters"], {"v": 1.0})
        self.assertEqual(hist["lifecycle_state"], "执行中")
        # 撤回日之后不再适用
        after = self.service.resolve(date(2026, 3, 15), BEV)
        self.assertFalse(after["matched"])
        self.assertIn("撤回", after["match_path"]["evaluated"][0]["eliminated_reason"])
        # 核算快照原样保留
        records = self.service.list_accountings("2025年度")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["parameters_snapshot"], {"v": 1.0})

        # 封存后历史仍可复现
        self.service.seal(AUDITOR, v1["version_id"])
        hist2 = self.service.resolve(date(2026, 2, 1), BEV)
        self.assertTrue(hist2["matched"])

    def test_emergency_correction_creates_new_version_only(self) -> None:
        v1 = self.publish(start="2026-01-01", parameters={"v": 1.0})
        self.clock.now = datetime(2026, 4, 1, 8, 0)
        corr = self.service.emergency_correction(
            AUDITOR, v1["version_id"], {"v": 1.1},
            effective_start="2026-04-01", reason="参数小数点勘误",
        )
        self.assertEqual(corr["version"], 2)
        self.assertEqual(corr["supersedes_version"], 1)
        # 原版本原封不动
        fresh_v1 = self.service.get_version(v1["version_id"])
        self.assertEqual(fresh_v1["parameters"], {"v": 1.0})
        # 无变化的勘误被拒绝
        with self.assertRaises(ValidationError):
            self.service.emergency_correction(
                AUDITOR, v1["version_id"], {"v": 1.0}, reason="无变化"
            )
        # 追溯性勘误被拒绝
        with self.assertRaises(RetroactiveDeniedError):
            self.service.emergency_correction(
                AUDITOR, v1["version_id"], {"v": 9.9},
                effective_start="2026-01-01", reason="想改历史",
            )
        # 走完发布
        self.service.submit(FILER, corr["version_id"])
        self.service.sign(ACCOUNTANT, corr["version_id"])
        self.service.sign(AUDITOR, corr["version_id"])
        self.service.publish(AUDITOR, corr["version_id"])
        # 勘误日之前仍是 v1，之后是 v2，历史核算参数不受影响
        self.assertEqual(self.service.resolve("2026-03-31", BEV)["parameters"], {"v": 1.0})
        self.assertEqual(self.service.resolve("2026-04-01", BEV)["parameters"], {"v": 1.1})

    def test_future_announcement_does_not_change_today(self) -> None:
        self.publish(start="2026-01-01", end="2027-01-01", parameters={"v": 1.0})
        future = self.service.announce_future(
            FILER, "BL-BEV", "低能耗基线", {"v": 3.0},
            [{"field": "energy_type", "operator": "eq", "value": "bev"}],
            "2027-01-01",
        )
        self.assertEqual(future["status"], "草稿")
        # 预告不参与解析
        self.assertEqual(self.service.resolve("2026-06-01", BEV)["parameters"], {"v": 1.0})
        result = self.service.resolve("2026-06-01", BEV)
        self.assertTrue(all(
            e["version_id"] != future["version_id"] or "未发布" in (e.get("eliminated_reason") or "")
            for e in result["match_path"]["evaluated"]
        ))
        # 预告起点必须严格晚于当前日期
        with self.assertRaises(ValidationError):
            self.service.announce_future(
                FILER, "BL-X", "t", {"v": 1},
                [{"field": "energy_type", "operator": "eq", "value": "bev"}],
                "2025-12-01",
            )

    def test_published_version_is_immutable(self) -> None:
        v1 = self.publish()
        with self.assertRaises(Exception):
            self.service.update_draft(FILER, v1["version_id"], parameters={"v": 9.0})


class AmbiguityDefenseTest(unittest.TestCase):
    def test_resolver_raises_on_two_active_versions(self) -> None:
        """直接构造两个同时命中的已发布版本，解析器必须报错而非静默选择。"""
        from baseline.model import RuleVersion

        def mk(number: int) -> RuleVersion:
            return RuleVersion(
                rule_code="X", version=number, title="t",
                parameters={"n": number},
                conditions=[{"field": "energy_type", "operator": "eq", "value": "bev"}],
                effective_start=date(2026, 1, 1), effective_end=None,
                supersedes_version=None, status="已确认",
                version_id=f"id{number}",
                published_at=datetime(2026, 1, 1),
            )

        with self.assertRaises(AmbiguousRuleError):
            resolve([mk(1), mk(2)], date(2026, 6, 1), BEV)


class ConcurrentPublishTest(unittest.TestCase):
    def _new_service(self) -> RuleService:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return RuleService(
            RuleRepository(Path(tmp.name) / "rules.db"),
            clock=FixedClock(datetime(2025, 12, 1, 9, 0, 0)),
        )

    def test_concurrent_conflicting_publishes_yield_single_valid_version(self) -> None:
        total_published = 0
        total_conflict = 0
        for iteration in range(5):
            service = self._new_service()
            # 同一规则的两条互相冲突待发布版本，两个线程同时发布
            drafts = []
            for k in range(2):
                d = service.create_draft(
                    FILER, "BL-C", "并发基线", {"v": float(k)},
                    [{"field": "energy_type", "operator": "eq", "value": "bev"}],
                    "2026-05-01",
                )
                service.submit(FILER, d["version_id"])
                service.sign(ACCOUNTANT, d["version_id"])
                service.sign(AUDITOR, d["version_id"])
                drafts.append(d["version_id"])

            barrier = Barrier(2)
            outcomes: list[str] = []

            def publish(version_id: str) -> None:
                barrier.wait()
                try:
                    service.publish(AUDITOR, version_id)
                    outcomes.append("published")
                except RuleConflictError:
                    outcomes.append("conflict")

            threads = [Thread(target=publish, args=(vid,)) for vid in drafts]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(sorted(outcomes), ["conflict", "published"])
            total_published += outcomes.count("published")
            total_conflict += outcomes.count("conflict")

            published = [v for v in service.list_versions() if v["status"] == "已确认"]
            self.assertEqual(len(published), 1)

        self.assertEqual(total_published, 5)
        self.assertEqual(total_conflict, 5)


if __name__ == "__main__":
    unittest.main()
