"""HTTP API（标准库实现，无第三方依赖）。

所有写操作的操作人在请求体 ``actor`` 字段中给出：
``{"actor": {"actor_id": "...", "name": "...", "role": "核算专员"}, ...}``。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .errors import BaselineError, ValidationError
from .model import Actor
from .repository import RuleRepository
from .service import RuleService


def _build_service(db_path: str, clock: Any = datetime.now) -> RuleService:
    return RuleService(RuleRepository(db_path), clock=clock)


class _Handler(BaseHTTPRequestHandler):
    service: RuleService  # 由 make_server 注入到类属性

    server_version = "BaselineRuleAPI/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
        return

    # ---- 基础收发 ----

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return value

    def _actor(self, body: dict[str, Any]) -> Actor:
        raw = body.get("actor")
        if not isinstance(raw, dict):
            raise ValidationError("缺少操作人 actor：{actor_id, name, role}")
        try:
            return Actor(
                actor_id=raw["actor_id"], name=raw["name"], role=raw["role"]
            )
        except KeyError as exc:
            raise ValidationError(f"actor 缺少字段：{exc.args[0]}") from exc

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def _dispatch(self, method: str) -> None:
        try:
            parts = urlsplit(self.path)
            path = parts.path.strip("/")
            query = parse_qs(parts.query)
            segments = path.split("/") if path else []
            body = self._read_json() if method in ("POST", "PATCH") else {}
            self._route(method, segments, query, body)
        except BaselineError as exc:
            self._send_json(exc.http_status, exc.to_dict())
        except Exception as exc:  # 防御：不泄露堆栈给客户端
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"code": "INTERNAL_ERROR", "message": str(exc)},
            )

    def _route(
        self,
        method: str,
        segments: list[str],
        query: dict[str, list[str]],
        body: dict[str, Any],
    ) -> None:
        s = self.service

        if method == "GET" and segments == ["api", "rules"]:
            self._send_json(200, {"rules": s.list_rules()})
            return

        if method == "POST" and segments == ["api", "rules", "drafts"]:
            self._send_json(201, s.create_draft(
                self._actor(body),
                body["rule_code"], body.get("title", ""),
                body["parameters"], body["conditions"],
                body["effective_start"], body.get("effective_end"),
                supersedes_version=body.get("supersedes_version"),
            ))
            return

        if method == "POST" and segments == ["api", "rules", "announce"]:
            self._send_json(201, s.announce_future(
                self._actor(body),
                body["rule_code"], body.get("title", ""),
                body["parameters"], body["conditions"],
                body["effective_start"], body.get("effective_end"),
                supersedes_version=body.get("supersedes_version"),
            ))
            return

        if (
            method == "GET"
            and len(segments) == 4
            and segments[:2] == ["api", "rules"]
            and segments[3] == "versions"
        ):
            self._send_json(200, {"versions": s.list_versions(segments[2])})
            return

        if method == "POST" and segments == ["api", "resolve"]:
            day = body.get("date") or query.get("date", [""])[0]
            vehicle = body.get("vehicle")
            if not isinstance(vehicle, dict):
                raise ValidationError("缺少 vehicle 车型属性对象")
            self._send_json(200, s.resolve(day, vehicle, body.get("rule_code")))
            return

        if method == "POST" and segments == ["api", "accountings"]:
            self._send_json(201, s.record_accounting(
                self._actor(body), body["period"],
                body["target_date"], body["vehicle"],
                accounting_id=body.get("accounting_id"),
            ))
            return

        if method == "GET" and segments == ["api", "accountings"]:
            period = query.get("period", [None])[0]
            self._send_json(200, {"accountings": s.list_accountings(period)})
            return

        # /api/versions/{id}/...
        match = re.fullmatch(r"api/versions/([^/]+)(/([a-z-]+))?", "/".join(segments))
        if match:
            version_id = match.group(1)
            action = match.group(3)
            self._version_route(method, version_id, action, body)
            return

        self._send_json(404, {"code": "NOT_FOUND", "message": f"无此路由：{self.path}"})

    def _version_route(
        self, method: str, version_id: str, action: str | None, body: dict[str, Any]
    ) -> None:
        s = self.service
        actor = self._actor(body) if method in ("POST", "PATCH") else None

        if action is None:
            if method == "GET":
                self._send_json(200, s.get_version(version_id))
                return
        elif action == "history" and method == "GET":
            self._send_json(200, {"events": s.history(version_id)})
            return
        elif method == "POST":
            if action == "submit":
                self._send_json(200, s.submit(actor, version_id))
                return
            if action == "return":
                self._send_json(200, s.return_for_revision(
                    actor, version_id, body.get("reason", "")))
                return
            if action == "sign":
                self._send_json(200, s.sign(actor, version_id, body.get("note", "")))
                return
            if action == "publish":
                self._send_json(200, s.publish(actor, version_id))
                return
            if action == "withdraw":
                self._send_json(200, s.withdraw(actor, version_id, body.get("reason", "")))
                return
            if action == "seal":
                self._send_json(200, s.seal(actor, version_id))
                return
            if action == "emergency-correction":
                self._send_json(201, s.emergency_correction(
                    actor, version_id, body["parameters"],
                    effective_start=body.get("effective_start"),
                    reason=body.get("reason", ""),
                ))
                return
        elif method == "PATCH" and action is None:
            self._send_json(200, s.update_draft(
                actor, version_id,
                parameters=body.get("parameters"),
                conditions=body.get("conditions"),
                effective_start=body.get("effective_start"),
                effective_end=body.get("effective_end"),
                title=body.get("title"),
            ))
            return

        self._send_json(404, {"code": "NOT_FOUND", "message": f"无此版本操作：{self.path}"})


def make_server(host: str, port: int, db_path: str, clock: Any = datetime.now) -> ThreadingHTTPServer:
    """构造可多线程并发访问的 API 服务（用于并发发布验证）。"""
    service = _build_service(db_path, clock=clock)

    handler = type("BoundHandler", (_Handler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.service = service  # 测试可直接取用
    return httpd


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="车型基线规则库 HTTP API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="baseline.db")
    args = parser.parse_args()

    httpd = make_server(args.host, args.port, args.db)
    print(f"车型基线规则库 API 监听 http://{args.host}:{args.port}（数据库 {args.db}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
