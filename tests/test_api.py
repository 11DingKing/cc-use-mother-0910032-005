"""HTTP API 端到端测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from baseline.api import make_server  # noqa: E402


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db = Path(self.tmp.name) / "api.db"
        self.clock = MutableClock(datetime(2025, 12, 1, 9, 0))
        self.httpd = make_server("127.0.0.1", 0, db, clock=self.clock)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def call(self, method: str, path: str, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_publish_resolve_conflict_history_freeze_flow(self) -> None:
        filer = {"actor_id": "e1", "name": "王申报", "role": "企业申报员"}
        accountant = {"actor_id": "a1", "name": "李核算", "role": "核算专员"}
        auditor = {"actor_id": "u1", "name": "赵审计", "role": "监管审计员"}
        bev_cond = [{"field": "energy_type", "operator": "eq", "value": "bev"}]

        status, draft = self.call("POST", "/api/rules/drafts", {
            "actor": filer, "rule_code": "BL-BEV", "title": "低能耗基线",
            "parameters": {"kwh": 12.5}, "conditions": bev_cond,
            "effective_start": "2026-01-01",
        })
        self.assertEqual(status, 201)
        vid = draft["version_id"]

        self.assertEqual(self.call("POST", f"/api/versions/{vid}/submit", {"actor": filer})[0], 200)
        self.assertEqual(self.call("POST", f"/api/versions/{vid}/sign",
                                   {"actor": accountant, "note": "ok"})[0], 200)
        self.assertEqual(self.call("POST", f"/api/versions/{vid}/sign",
                                   {"actor": auditor})[0], 200)
        status, published = self.call("POST", f"/api/versions/{vid}/publish", {"actor": auditor})
        self.assertEqual(status, 200)
        self.assertEqual(published["status"], "已确认")

        # 解析
        status, result = self.call("POST", "/api/resolve", {
            "date": "2026-06-01",
            "vehicle": {"energy_type": "bev", "curb_weight_kg": 1600},
        })
        self.assertEqual(status, 200)
        self.assertTrue(result["matched"])
        self.assertEqual(result["parameters"], {"kwh": 12.5})
        self.assertIn("filters", result["match_path"])

        # 冲突发布被 409 拒绝
        _, draft2 = self.call("POST", "/api/rules/drafts", {
            "actor": filer, "rule_code": "BL-BEV", "title": "低能耗基线",
            "parameters": {"kwh": 99.0}, "conditions": bev_cond,
            "effective_start": "2026-03-01",
        })
        vid2 = draft2["version_id"]
        self.call("POST", f"/api/versions/{vid2}/submit", {"actor": filer})
        self.call("POST", f"/api/versions/{vid2}/sign", {"actor": accountant})
        self.call("POST", f"/api/versions/{vid2}/sign", {"actor": auditor})
        status, err = self.call("POST", f"/api/versions/{vid2}/publish", {"actor": auditor})
        self.assertEqual(status, 409)
        self.assertEqual(err["code"], "RULE_CONFLICT")

        # 撤回后历史仍可解析，撤回日之后失效
        self.clock.now = datetime(2026, 6, 2, 10, 0)
        status, _ = self.call("POST", f"/api/versions/{vid}/withdraw",
                              {"actor": auditor, "reason": "口径调整"})
        self.assertEqual(status, 200)
        _, hist = self.call("POST", "/api/resolve", {
            "date": "2026-06-02", "vehicle": {"energy_type": "bev"}})
        self.assertFalse(hist["matched"])
        _, past = self.call("POST", "/api/resolve", {
            "date": "2026-06-01", "vehicle": {"energy_type": "bev"}})
        self.assertTrue(past["matched"])
        self.assertEqual(past["parameters"], {"kwh": 12.5})

        # 审计历史
        status, events = self.call("GET", f"/api/versions/{vid}/history")
        self.assertEqual(status, 200)
        names = {e["event"] for e in events["events"]}
        self.assertIn("publish", names)
        self.assertIn("withdraw", names)

    def test_validation_error_shape(self) -> None:
        status, err = self.call("POST", "/api/rules/drafts", {
            "actor": {"actor_id": "e1", "name": "王申报", "role": "企业申报员"},
            "rule_code": "X", "parameters": {}, "conditions": [],
            "effective_start": "2026-01-01",
        })
        self.assertEqual(status, 400)
        self.assertEqual(err["code"], "VALIDATION_ERROR")


if __name__ == "__main__":
    unittest.main()
