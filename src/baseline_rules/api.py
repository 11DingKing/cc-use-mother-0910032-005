"""基于标准库 ``http.server`` 的 HTTP API。

线程模型：``ThreadingHTTPServer`` + 服务内全局锁，并发发布在服务层
串行化，竞争草稿只有一个能成功，其余得到 409。

路由：

- ``POST /v1/publish``             发布（kind=regular|errata|preview）
- ``POST /v1/precheck``            仅发布前检测（区间重叠/条件冲突）
- ``POST /v1/withdraw``            签署撤回
- ``GET  /v1/resolve?date=&vehicle=``  按日期+车型解析唯一规则并解释路径
- ``GET  /v1/versions[?rule_id=]`` 列出已发布版本（不可变）
- ``GET  /v1/versions/{id}``       取单个版本
- ``GET  /v1/withdrawals/{id}``    查询撤回签署
- ``GET  /v1/events``              追加事件审计链
- ``GET  /v1/snapshot``            完整归档快照
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from .errors import (
    AmbiguousRuleError,
    BaselineRuleError,
    ConflictError,
    InvalidRuleContent,
    NoApplicableRuleError,
    NotFoundError,
)
from .models import Criterion
from .service import BaselineRuleService

_SERVICE_LOCK = threading.Lock()
_SERVICE: BaselineRuleService | None = None


def configure_service(service: BaselineRuleService) -> None:
    """注入全局服务实例（测试或装配时使用）。"""
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = service


def get_service() -> BaselineRuleService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = BaselineRuleService()
        return _SERVICE


def _draft_from_payload(service: BaselineRuleService, payload: dict[str, Any]):
    try:
        return service.draft(
            rule_id=payload["rule_id"],
            effective_from=payload["effective_from"],
            effective_to=payload.get("effective_to"),
            criteria=[Criterion.from_dict(c) for c in payload["criteria"]],
            parameters=payload["parameters"],
            kind=payload.get("kind", "regular"),
            supersedes=payload.get("supersedes"),
            basis=payload.get("basis"),
        )
    except KeyError as exc:
        raise InvalidRuleContent(f"请求缺少字段：{exc.args[0]}") from None
    except TypeError:
        raise InvalidRuleContent("criteria 必须是条件对象列表") from None


_ERROR_STATUS = {
    InvalidRuleContent: (422, "invalid_content"),
    ConflictError: (409, "conflict"),
    NotFoundError: (404, "not_found"),
    NoApplicableRuleError: (404, "no_applicable_rule"),
    AmbiguousRuleError: (409, "ambiguous"),
}


def build_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    return server


class _Handler(BaseHTTPRequestHandler):
    server_version = "BaselineRules/1.0"

    # -- 工具 -------------------------------------------------------------

    def _send_json(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise InvalidRuleContent("请求体必须是 JSON 对象")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise InvalidRuleContent("请求体不是合法 JSON")
        if not isinstance(value, dict):
            raise InvalidRuleContent("请求体必须是 JSON 对象")
        return value

    def _dispatch(self, fn: Callable[[], Any], success_status: int = 200) -> None:
        try:
            self._send_json(success_status, fn())
        except ConflictError as exc:
            self._send_json(409, {"error": "conflict", "message": str(exc), "conflicts": exc.conflicts})
        except BaselineRuleError as exc:
            status, code = _ERROR_STATUS[type(exc)]
            self._send_json(status, {"error": code, "message": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return  # 静默；由装配层按需接入审计日志

    # -- 路由 -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path.rstrip("/") or "/")
        query = parse_qs(parsed.query)
        service = get_service()

        if path == "/v1/resolve":
            self._dispatch(lambda: self._resolve(service, query))
            return
        if path == "/v1/versions":
            rule_id = query.get("rule_id", [None])[0]
            self._dispatch(
                lambda: {
                    "versions": [
                        v.to_dict()
                        for v in (service.list_versions(rule_id) if rule_id else service.list_all_versions())
                    ]
                }
            )
            return
        if path == "/v1/events":
            self._dispatch(lambda: {"events": [e.to_dict() for e in service.events()]})
            return
        if path == "/v1/snapshot":
            self._dispatch(service.snapshot)
            return
        m = re.fullmatch(r"/v1/versions/([^/]+)", path)
        if m:
            self._dispatch(lambda: service.get_version(m.group(1)).to_dict())
            return
        m = re.fullmatch(r"/v1/withdrawals/([^/]+)", path)
        if m:
            self._dispatch(lambda: self._withdrawal(service, m.group(1)))
            return
        self._send_json(404, {"error": "not_found", "message": f"未知路径：{path}"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        service = get_service()
        if path == "/v1/precheck":
            self._dispatch(lambda: service.precheck(_draft_from_payload(service, self._read_json())))
            return
        if path == "/v1/publish":
            def do_publish() -> dict[str, Any]:
                payload = self._read_json()
                version = service.publish(
                    _draft_from_payload(service, payload),
                    signer=str(payload.get("signer", "")),
                )
                return {"published": version.to_dict()}
            self._dispatch(do_publish, success_status=201)
            return
        if path == "/v1/withdraw":
            def do_withdraw() -> dict[str, Any]:
                payload = self._read_json()
                try:
                    record = service.withdraw(
                        version_id=payload["version_id"],
                        signer=str(payload.get("signer", "")),
                        reason=str(payload.get("reason", "")),
                        effective_from=payload.get("effective_from"),
                    )
                except KeyError as exc:
                    raise InvalidRuleContent(f"请求缺少字段：{exc.args[0]}") from None
                return {"withdrawal": record.to_dict()}
            self._dispatch(do_withdraw, success_status=201)
            return
        self._send_json(404, {"error": "not_found", "message": f"未知路径：{path}"})

    # -- 具体处理 ---------------------------------------------------------

    def _resolve(self, service: BaselineRuleService, query: dict[str, list[str]]) -> dict[str, Any]:
        try:
            day = query["date"][0]
            vehicle_raw = query["vehicle"][0]
        except (KeyError, IndexError):
            raise InvalidRuleContent("resolve 需要 date 与 vehicle 查询参数")
        try:
            vehicle = json.loads(vehicle_raw)
        except json.JSONDecodeError:
            raise InvalidRuleContent("vehicle 必须是 JSON 编码的车型档案")
        if not isinstance(vehicle, dict):
            raise InvalidRuleContent("vehicle 必须是 JSON 对象")
        result = service.resolve(day, vehicle)
        body = result.to_dict()
        body["message"] = (
            "解析到唯一有效规则"
            if result.matched
            else "该日期与车型无适用规则"
            if result.status == "none"
            else "存在多个候选有效规则，发布门禁数据可能已被破坏"
        )
        return body

    def _withdrawal(self, service: BaselineRuleService, version_id: str) -> dict[str, Any]:
        record = service.get_withdrawal(version_id)
        if record is None:
            raise NotFoundError(f"版本 {version_id} 无撤回记录")
        return {"version_id": version_id, "withdrawal": record.to_dict()}


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """命令行入口：阻塞运行 HTTP 服务。"""
    httpd = build_server(host, port)
    print(f"车型基线规则库 API 运行于 http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    import sys

    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    serve(host, port)
