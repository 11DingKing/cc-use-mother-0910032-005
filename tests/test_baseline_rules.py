"""基线规则库后端回归测试。

覆盖领域契约的四大不变量：
规则生效区间、条件冲突检测、历史适用冻结、并发唯一发布，
以及撤回、紧急勘误、未来版本预告与按日期解析的匹配路径解释。
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
from datetime import date
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from baseline_rules import (
    AmbiguousRuleError,
    BaselineRuleService,
    ConflictError,
    InvalidRuleContent,
    NoApplicableRuleError,
    NotFoundError,
)
from baseline_rules.api import build_server, configure_service
from baseline_rules.conflict import jointly_satisfiable, same_coverage

BEV = {"energy_type": "bev", "weight_class": 2}
HEV = {"energy_type": "hev", "weight_class": 2}

CRIT_BEV = [{"field": "energy_type", "op": "eq", "value": "bev"}]
CRIT_HEV = [{"field": "energy_type", "op": "eq", "value": "hev"}]
CRIT_ANY_EV = [{"field": "energy_type", "op": "in", "value": ["bev", "hev"]}]
PARAMS_V1 = {"baseline_kwh_per_100km": 12.5, "factor": 1.0}
PARAMS_V2 = {"baseline_kwh_per_100km": 11.8, "factor": 0.9}


class ServiceTestBase(unittest.TestCase):
    def make_service(self, today: str = "2026-01-10") -> BaselineRuleService:
        return BaselineRuleService(today=date.fromisoformat(today))

    def publish(self, svc: BaselineRuleService, **overrides):
        payload = {
            "rule_id": "LOW_ENERGY",
            "effective_from": "2026-02-01",
            "criteria": CRIT_BEV,
            "parameters": PARAMS_V1,
        }
        payload.update(overrides)
        draft = svc.draft(**payload)
        return svc.publish(draft, signer="政策部门-张")


class TemporalWindowTest(ServiceTestBase):
    def test_closed_window_boundaries_are_inclusive(self) -> None:
        svc = self.make_service()
        v = self.publish(svc, effective_from="2026-02-01", effective_to="2026-06-30")
        self.assertTrue(v.window_covers(date(2026, 2, 1)))
        self.assertTrue(v.window_covers(date(2026, 6, 30)))
        self.assertFalse(v.window_covers(date(2026, 1, 31)))
        self.assertFalse(v.window_covers(date(2026, 7, 1)))

    def test_open_ended_window_extends_forward(self) -> None:
        svc = self.make_service()
        v = self.publish(svc, effective_to=None)
        self.assertTrue(v.window_covers(date(2099, 1, 1)))

    def test_rejects_reversed_window(self) -> None:
        svc = self.make_service()
        with self.assertRaises(InvalidRuleContent):
            self.publish(svc, effective_from="2026-06-01", effective_to="2026-02-01")

    def test_no_retroactive_effective_date(self) -> None:
        svc = self.make_service(today="2026-03-15")
        with self.assertRaises(InvalidRuleContent):
            self.publish(svc, effective_from="2026-03-01")

    def test_invalid_criteria_and_parameters(self) -> None:
        svc = self.make_service()
        with self.assertRaises(InvalidRuleContent):
            svc.publish(svc.draft("R", "2026-02-01", [], PARAMS_V1), "s")
        with self.assertRaises(InvalidRuleContent):
            svc.publish(svc.draft("R", "2026-02-01", CRIT_BEV, {}), "s")
        with self.assertRaises(InvalidRuleContent):
            svc.draft("R", "2026-02-01", [{"field": "nope", "op": "eq", "value": 1}], PARAMS_V1)

    def test_self_contradictory_criteria_rejected(self) -> None:
        svc = self.make_service()
        crit = [
            {"field": "energy_type", "op": "eq", "value": "bev"},
            {"field": "energy_type", "op": "eq", "value": "hev"},
        ]
        with self.assertRaises(InvalidRuleContent):
            svc.draft("R", "2026-02-01", crit, PARAMS_V1)


class ConflictDetectionTest(ServiceTestBase):
    def test_overlapping_compatible_conditions_rejected(self) -> None:
        svc = self.make_service()
        self.publish(svc, criteria=CRIT_ANY_EV, effective_from="2026-02-01", effective_to="2026-12-31")
        with self.assertRaises(ConflictError) as ctx:
            self.publish(svc, rule_id="OTHER", criteria=CRIT_BEV,
                         effective_from="2026-06-01", effective_to="2026-08-31")
        self.assertEqual(len(ctx.exception.conflicts), 1)
        self.assertIn("LOW_ENERGY#v1", ctx.exception.conflicts[0]["other"])

    def test_overlapping_mutually_exclusive_conditions_allowed(self) -> None:
        svc = self.make_service()
        self.publish(svc, criteria=CRIT_BEV, effective_to="2026-12-31")
        second = self.publish(svc, rule_id="HEV_RULE", criteria=CRIT_HEV,
                              effective_from="2026-02-01", effective_to="2026-12-31")
        self.assertEqual(second.version, 1)
        # 两车型在重叠期各自唯一解析
        self.assertEqual(svc.resolve_required("2026-05-01", BEV).rule_id, "LOW_ENERGY")
        self.assertEqual(svc.resolve_required("2026-05-01", HEV).rule_id, "HEV_RULE")

    def test_numeric_range_mutex_detected(self) -> None:
        light = [{"field": "weight_class", "op": "lte", "value": 2}]
        heavy = [{"field": "weight_class", "op": "gte", "value": 3}]
        self.assertFalse(jointly_satisfiable(light, heavy)[0])
        middle = [{"field": "weight_class", "op": "in", "value": [2, 3]}]
        self.assertTrue(jointly_satisfiable(light, middle)[0])
        svc = self.make_service()
        self.publish(svc, rule_id="LIGHT", criteria=light, effective_to="2026-12-31")
        self.publish(svc, rule_id="HEAVY", criteria=heavy, effective_to="2026-12-31")
        self.assertEqual(svc.resolve_required("2026-05-01", {"weight_class": 1}).rule_id, "LIGHT")
        self.assertEqual(svc.resolve_required("2026-05-01", {"weight_class": 5}).rule_id, "HEAVY")

    def test_conflict_against_only_future_open_segment(self) -> None:
        # 已发布版本在 2026-09 到期，开放区间草稿与它只在到期日前冲突
        svc = self.make_service()
        self.publish(svc, criteria=CRIT_BEV,
                     effective_from="2026-02-01", effective_to="2026-08-31")
        self.publish(svc, rule_id="FINE", criteria=CRIT_BEV,
                     effective_from="2026-09-01", effective_to=None)
        with self.assertRaises(ConflictError):
            self.publish(svc, rule_id="BAD", criteria=CRIT_BEV,
                         effective_from="2026-05-01", effective_to=None)

    def test_precheck_reports_conflicts_and_coexistence(self) -> None:
        svc = self.make_service()
        self.publish(svc, criteria=CRIT_BEV, effective_to="2026-12-31")
        draft = svc.draft(rule_id="HEV_RULE", effective_from="2026-03-01",
                          criteria=CRIT_HEV, parameters=PARAMS_V1)
        report = svc.precheck(draft)
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(len(report["coexisting"]), 1)
        self.assertIn("互斥", report["coexisting"][0]["reason"])

    def test_supersedes_same_coverage_rule(self) -> None:
        svc = self.make_service()
        self.publish(svc)
        svc.set_today("2026-07-01")
        with self.assertRaises(InvalidRuleContent):
            svc.errata("LOW_ENERGY#v1", CRIT_ANY_EV, PARAMS_V2,
                       signer="监管-李", basis="参数表勘误", effective_from="2026-07-01")
        with self.assertRaises(InvalidRuleContent):
            svc.errata("LOW_ENERGY#v1", CRIT_BEV, PARAMS_V2,
                       signer="监管-李", basis="  ", effective_from="2026-07-01")
        ok, _ = same_coverage(CRIT_BEV, [{"field": "energy_type", "op": "in", "value": ["bev"]}])
        self.assertTrue(ok)

    def test_supersedes_crosses_rule_family(self) -> None:
        svc = self.make_service()
        self.publish(svc)
        draft = svc.draft("OTHER", "2026-02-01", CRIT_BEV, PARAMS_V2,
                          supersedes="LOW_ENERGY#v1")
        with self.assertRaises(InvalidRuleContent):
            svc.publish(draft, "s")


class HistoryFreezeTest(ServiceTestBase):
    def _v1(self, svc: BaselineRuleService):
        return self.publish(svc, effective_from="2026-01-01", effective_to="2026-12-31")

    def test_errata_does_not_rewrite_past_resolution(self) -> None:
        svc = self.make_service(today="2026-01-01")
        v1 = self._v1(svc)
        before = svc.resolve_required("2026-03-01", BEV)
        self.assertIs(before, v1)

        svc.set_today("2026-07-01")
        v2 = svc.errata("LOW_ENERGY#v1", CRIT_BEV, PARAMS_V2,
                        signer="监管-李", basis="能耗参数表第 3 行登载错误",
                        effective_from="2026-07-01")
        self.assertEqual(v2.kind, "errata")
        self.assertEqual(v2.supersedes, "LOW_ENERGY#v1")

        # 历史日期仍解析到 v1，且签名不变——已完成的年度申报不受影响
        past = svc.resolve_required("2026-03-01", BEV)
        self.assertEqual(past.version_id, "LOW_ENERGY#v1")
        self.assertEqual(past.signature, v1.signature)
        self.assertEqual(past.parameters["factor"], 1.0)
        # 勘误生效日起解析到 v2
        future = svc.resolve_required("2026-08-01", BEV)
        self.assertEqual(future.version_id, "LOW_ENERGY#v2")
        self.assertEqual(future.parameters["factor"], 0.9)
        self.assertEqual(future.supersedes, "LOW_ENERGY#v1")

    def test_errata_cannot_be_retroactive(self) -> None:
        svc = self.make_service(today="2026-01-01")
        self._v1(svc)
        svc.set_today("2026-07-01")
        with self.assertRaises(InvalidRuleContent):
            svc.errata("LOW_ENERGY#v1", CRIT_BEV, PARAMS_V2,
                       signer="监管-李", basis="x", effective_from="2026-03-01")

    def test_withdrawal_only_truncates_future(self) -> None:
        svc = self.make_service(today="2026-01-01")
        v1 = self._v1(svc)
        svc.set_today("2026-09-01")
        svc.withdraw("LOW_ENERGY#v1", signer="交易运营-王",
                     reason="基线停止执行", effective_from="2026-09-15")
        # 撤回生效前仍可解析
        self.assertEqual(svc.resolve_required("2026-09-14", BEV).version_id, v1.version_id)
        # 撤回生效后无规则
        with self.assertRaises(NoApplicableRuleError):
            svc.resolve_required("2026-09-15", BEV)
        # 历史日期永远不受影响
        self.assertEqual(svc.resolve_required("2026-04-01", BEV).version_id, v1.version_id)
        # 记录不可变、不可重复撤回
        with self.assertRaises(InvalidRuleContent):
            svc.withdraw("LOW_ENERGY#v1", signer="交易运营-王", reason="再次撤回")
        with self.assertRaises(InvalidRuleContent):
            svc.withdraw("LOW_ENERGY#v1", signer="交易运营-王",
                         reason="追溯撤回", effective_from="2026-08-01")
        self.assertIsNotNone(svc.get_withdrawal("LOW_ENERGY#v1"))

    def test_withdrawing_superseding_version_revives_past_but_not_history(self) -> None:
        svc = self.make_service(today="2026-01-01")
        self._v1(svc)
        svc.set_today("2026-07-01")
        self.publish(svc, effective_from="2026-07-01", effective_to="2026-12-31",
                     parameters=PARAMS_V2, supersedes="LOW_ENERGY#v1")
        # v1 的窗口止于年底；v2 在 10 月被撤回 → 10 月起由 v1 治理（其窗口仍覆盖）
        svc.set_today("2026-10-01")
        svc.withdraw("LOW_ENERGY#v2", signer="监管-李",
                     reason="勘误复核未通过", effective_from="2026-10-05")
        self.assertEqual(svc.resolve_required("2026-09-01", BEV).version_id, "LOW_ENERGY#v2")
        self.assertEqual(svc.resolve_required("2026-10-10", BEV).version_id, "LOW_ENERGY#v1")

    def test_preview_never_participates_in_current_resolution(self) -> None:
        svc = self.make_service(today="2026-01-01")
        self._v1(svc)
        preview = svc.preview("LOW_ENERGY", "2027-01-01", CRIT_BEV, PARAMS_V2,
                              signer="政策部门-张", basis="2027 年度基线预告",
                              effective_to="2027-12-31")
        self.assertEqual(preview.kind, "preview")
        self.assertEqual(svc.resolve_required("2026-06-01", BEV).version_id, "LOW_ENERGY#v1")
        result = svc.resolve("2027-06-01", BEV)
        self.assertEqual(result.status, "none")
        preview_entries = [t for t in result.trace if t["version_id"] == preview.version_id]
        self.assertTrue(preview_entries[0]["eliminated"])
        self.assertIn("预告", preview_entries[0]["elimination_reason"])
        # 预告必须是严格未来的窗口（当日或过去的起始日被拒）
        with self.assertRaises(InvalidRuleContent):
            svc.preview("LOW_ENERGY", "2026-01-01", CRIT_BEV, PARAMS_V2,
                        signer="政策部门-张", basis="x")
        # 与预告冲突的常规发布被拒；互斥条件的分治发布允许
        with self.assertRaises(ConflictError):
            self.publish(svc, rule_id="CLASH", criteria=CRIT_ANY_EV,
                         effective_from="2027-03-01", effective_to="2027-06-30")
        self.publish(svc, rule_id="HEV_2027", criteria=CRIT_HEV,
                     effective_from="2027-01-01", effective_to="2027-12-31")

    def test_preview_is_carried_by_regular_publish(self) -> None:
        svc = self.make_service(today="2026-01-10")
        pv = svc.preview("LOW_ENERGY", "2027-01-01", CRIT_BEV, PARAMS_V1,
                         signer="政策部门-张", basis="2027 预告", effective_to="2027-12-31")
        svc.set_today("2026-12-20")
        official = self.publish(
            svc, effective_from="2027-01-01", effective_to="2027-12-31",
            parameters=PARAMS_V2, supersedes=pv.version_id)
        self.assertEqual(official.kind, "regular")
        self.assertEqual(svc.resolve_required("2027-06-01", BEV).version_id, official.version_id)


class ResolveTraceTest(ServiceTestBase):
    def test_trace_explains_every_elimination(self) -> None:
        svc = self.make_service(today="2026-01-10")
        v1 = self.publish(svc, effective_from="2026-02-01", effective_to="2026-12-31")
        result = svc.resolve("2026-05-01", BEV)
        self.assertEqual(result.status, "unique")
        self.assertIs(result.version, v1)
        entry = result.trace[0]
        self.assertFalse(entry["eliminated"])
        checks = {s["check"]: s for s in entry["steps"]}
        self.assertTrue(checks["生效窗口"]["passed"])
        self.assertTrue(checks["适用车型条件"]["passed"])
        self.assertTrue(checks["替代关系"]["passed"])

    def test_trace_for_non_matching_vehicle(self) -> None:
        svc = self.make_service()
        self.publish(svc)
        result = svc.resolve("2026-05-01", HEV)
        self.assertEqual(result.status, "none")
        entry = result.trace[0]
        self.assertTrue(entry["eliminated"])
        self.assertIn("适用条件", entry["elimination_reason"])
        crit_step = next(s for s in entry["steps"] if s["check"] == "适用车型条件")
        self.assertFalse(crit_step["criteria"][0]["passed"])
        self.assertEqual(crit_step["criteria"][0]["actual"], "hev")

    def test_no_rule_outside_any_window(self) -> None:
        svc = self.make_service()
        self.publish(svc, effective_from="2026-02-01", effective_to="2026-03-01")
        with self.assertRaises(NoApplicableRuleError):
            svc.resolve_required("2027-01-01", BEV)

    def test_unknown_version_and_bad_date(self) -> None:
        svc = self.make_service()
        with self.assertRaises(NotFoundError):
            svc.get_version("GHOST#v9")
        with self.assertRaises(InvalidRuleContent):
            svc.resolve("2026/02/01", BEV)


class ConcurrencyTest(ServiceTestBase):
    def test_concurrent_competing_publishes_yield_single_valid_version(self) -> None:
        svc = self.make_service()
        # 两个部门同时发布同期、同车型条件的规则
        barrier = threading.Barrier(8)

        def attempt(rule_id: str, out: list) -> None:
            barrier.wait()
            try:
                draft = svc.draft(rule_id, "2026-02-01", CRIT_BEV, PARAMS_V1,
                                  effective_to="2026-12-31")
                out.append(svc.publish(draft, signer=f"签署人-{rule_id}"))
            except ConflictError as exc:
                out.append(exc)

        threads: list[threading.Thread] = []
        results: list = []
        for i in range(8):
            t = threading.Thread(target=attempt, args=(f"R{i}", results))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        published = [r for r in results if not isinstance(r, ConflictError)]
        rejected = [r for r in results if isinstance(r, ConflictError)]
        self.assertEqual(len(published), 1)
        self.assertEqual(len(rejected), 7)
        # 任何日期、车型都至多解析出一条
        result = svc.resolve("2026-06-01", BEV)
        self.assertEqual(result.status, "unique")

    def test_concurrent_errata_chain_is_consistent(self) -> None:
        svc = self.make_service(today="2026-01-01")
        self.publish(svc, effective_from="2026-01-01", effective_to="2026-12-31")
        svc.set_today("2026-07-01")
        barrier = threading.Barrier(4)

        def attempt(out: list) -> None:
            barrier.wait()
            try:
                out.append(svc.errata("LOW_ENERGY#v1", CRIT_BEV, PARAMS_V2,
                                      signer="监管-李", basis="并发勘误",
                                      effective_from="2026-07-01"))
            except ConflictError as exc:
                out.append(exc)

        results: list = []
        threads = [threading.Thread(target=attempt, args=(results,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len([r for r in results if not isinstance(r, ConflictError)]), 1)
        self.assertEqual(svc.resolve_required("2026-08-01", BEV).version_id,
                         "LOW_ENERGY#v2")
        self.assertEqual(svc.resolve_required("2026-06-01", BEV).version_id,
                         "LOW_ENERGY#v1")


class SnapshotTest(ServiceTestBase):
    def test_snapshot_roundtrip_preserves_history(self) -> None:
        svc = self.make_service(today="2026-01-01")
        v1 = self.publish(svc, effective_from="2026-01-01", effective_to="2026-12-31")
        svc.set_today("2026-07-01")
        svc.errata("LOW_ENERGY#v1", CRIT_BEV, PARAMS_V2, signer="监管-李",
                   basis="b", effective_from="2026-07-01")
        svc.withdraw("LOW_ENERGY#v2", signer="监管-李", reason="r",
                     effective_from="2026-10-01")
        svc.withdraw("LOW_ENERGY#v1", signer="监管-李", reason="r",
                     effective_from="2026-10-01")
        data = svc.snapshot()
        restored = BaselineRuleService.restore_snapshot(data, today=date(2026, 10, 2))
        self.assertEqual(restored.resolve_required("2026-03-01", BEV).signature, v1.signature)
        self.assertEqual(restored.resolve_required("2026-08-01", BEV).version_id,
                         "LOW_ENERGY#v2")
        with self.assertRaises(NoApplicableRuleError):
            restored.resolve_required("2026-10-02", BEV)
        self.assertEqual(len(restored.events()), len(svc.events()))

    def test_tampered_snapshot_rejected(self) -> None:
        svc = self.make_service()
        self.publish(svc)
        data = svc.snapshot()
        data["versions"][0]["parameters"]["factor"] = 0.01
        with self.assertRaises(InvalidRuleContent):
            BaselineRuleService.restore_snapshot(data)


class ApiTest(ServiceTestBase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = BaselineRuleService(today=date(2026, 1, 10))
        configure_service(cls.service)
        cls.server = build_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _post(self, path: str, body: dict):
        req = urllib.request.Request(
            self._url(path), data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _get(self, path: str):
        try:
            with urllib.request.urlopen(self._url(path), timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_lifecycle_over_http(self) -> None:
        body = {
            "rule_id": "LOW_ENERGY", "effective_from": "2026-02-01",
            "effective_to": "2026-12-31", "criteria": CRIT_BEV,
            "parameters": PARAMS_V1, "signer": "政策部门-张",
        }
        status, resp = self._post("/v1/publish", body)
        self.assertEqual(status, 201)
        self.assertEqual(resp["published"]["version_id"], "LOW_ENERGY#v1")
        self.assertIn("signature", resp["published"])

        # 冲突发布 → 409，携带冲突明细
        clash = dict(body, rule_id="CLASH", criteria=CRIT_ANY_EV)
        status, resp = self._post("/v1/publish", clash)
        self.assertEqual(status, 409)
        self.assertEqual(resp["error"], "conflict")
        self.assertTrue(resp["conflicts"])

        # 互斥条件 → precheck 通过
        status, resp = self._post("/v1/precheck", dict(body, rule_id="HEV_RULE", criteria=CRIT_HEV))
        self.assertEqual(status, 200)
        self.assertEqual(resp["conflicts"], [])

        # 按日期+车型解析，含匹配路径
        qv = quote(json.dumps(BEV, ensure_ascii=False))
        status, resp = self._get(f"/v1/resolve?date=2026-05-01&vehicle={qv}")
        self.assertEqual(status, 200)
        self.assertEqual(resp["status"], "unique")
        self.assertEqual(resp["rule"]["version_id"], "LOW_ENERGY#v1")
        self.assertTrue(resp["match_path"])

        # 撤回
        status, resp = self._post("/v1/withdraw", {
            "version_id": "LOW_ENERGY#v1", "signer": "交易运营-王",
            "reason": "停止执行", "effective_from": "2026-11-01"})
        self.assertEqual(status, 201)
        status, resp = self._get("/v1/withdrawals/LOW_ENERGY%23v1")
        self.assertEqual(status, 200)

        # 事件链与快照
        status, resp = self._get("/v1/events")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(resp["events"]), 2)
        status, resp = self._get("/v1/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(len(resp["versions"]), 1)


if __name__ == "__main__":
    unittest.main()
