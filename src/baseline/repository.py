"""SQLite 存储层。

所有写操作在 ``BEGIN IMMEDIATE`` 事务中完成：SQLite 只允许一个
保留写锁，因此并发发布会被数据库自动串行化，后进入事务的发布
请求在同一事务内重新读取已提交数据并执行冲突检测，从根本上保证
“同一日期 + 同一车型至多一个有效版本”。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .model import Actor, RuleVersion

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    rule_code      TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    latest_version INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    version_id          TEXT PRIMARY KEY,
    rule_code           TEXT NOT NULL,
    version             INTEGER NOT NULL,
    title               TEXT NOT NULL,
    parameters          TEXT NOT NULL,
    conditions          TEXT NOT NULL,
    effective_start     TEXT NOT NULL,
    effective_end       TEXT,
    supersedes_version  INTEGER,
    status              TEXT NOT NULL,
    draft_owner         TEXT,
    signatures          TEXT NOT NULL,
    published_at        TEXT,
    withdrawn_at        TEXT,
    withdraw_reason     TEXT,
    sealed_at           TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE(rule_code, version),
    FOREIGN KEY(rule_code) REFERENCES rules(rule_code)
);

CREATE INDEX IF NOT EXISTS idx_versions_rule ON versions(rule_code, version);
CREATE INDEX IF NOT EXISTS idx_versions_status ON versions(status);

CREATE TABLE IF NOT EXISTS audit_events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT NOT NULL,
    event      TEXT NOT NULL,
    actor      TEXT NOT NULL,
    at         TEXT NOT NULL,
    detail     TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_version ON audit_events(version_id, seq);

-- 年度申报核算快照：只增不改，用于证明历史核算不被后续规则变动改写
CREATE TABLE IF NOT EXISTS accountings (
    accounting_id       TEXT PRIMARY KEY,
    period              TEXT NOT NULL,
    vehicle             TEXT NOT NULL,
    target_date         TEXT NOT NULL,
    rule_code           TEXT NOT NULL,
    version             INTEGER NOT NULL,
    version_id          TEXT NOT NULL,
    parameters_snapshot TEXT NOT NULL,
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accountings_period ON accountings(period, accounting_id);
"""


def _connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


class RuleRepository:
    """规则库仓储；每个操作使用独立连接，可被多线程并发调用。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        first = _connect(self.path)
        try:
            first.executescript(SCHEMA)
        finally:
            first.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """立即获取写锁的事务上下文。"""
        conn = _connect(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read_only(self) -> Iterator[sqlite3.Connection]:
        conn = _connect(self.path)
        try:
            yield conn
        finally:
            conn.close()

    # ---- rules ----

    def get_rule(self, conn: sqlite3.Connection, rule_code: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM rules WHERE rule_code=?", (rule_code,)
        ).fetchone()

    def list_rules(self) -> list[sqlite3.Row]:
        with self.read_only() as conn:
            return list(conn.execute("SELECT * FROM rules ORDER BY rule_code"))

    def insert_rule(
        self, conn: sqlite3.Connection, rule_code: str, title: str, now: datetime
    ) -> None:
        conn.execute(
            "INSERT INTO rules(rule_code, title, latest_version, created_at, updated_at)"
            " VALUES(?,?,0,?,?)",
            (rule_code, title, now.isoformat(), now.isoformat()),
        )

    def bump_rule(
        self, conn: sqlite3.Connection, rule_code: str, version: int, now: datetime
    ) -> None:
        conn.execute(
            "UPDATE rules SET latest_version=?, updated_at=? WHERE rule_code=?",
            (version, now.isoformat(), rule_code),
        )

    # ---- versions ----

    @staticmethod
    def _row_to_version(row: sqlite3.Row) -> RuleVersion:
        return RuleVersion.from_row(dict(row))

    def get_version(
        self, conn: sqlite3.Connection, version_id: str
    ) -> RuleVersion | None:
        row = conn.execute(
            "SELECT * FROM versions WHERE version_id=?", (version_id,)
        ).fetchone()
        return self._row_to_version(row) if row else None

    def get_version_by_number(
        self, conn: sqlite3.Connection, rule_code: str, version: int
    ) -> RuleVersion | None:
        row = conn.execute(
            "SELECT * FROM versions WHERE rule_code=? AND version=?",
            (rule_code, version),
        ).fetchone()
        return self._row_to_version(row) if row else None

    def list_versions(self, rule_code: str | None = None) -> list[RuleVersion]:
        with self.read_only() as conn:
            return self.list_versions_in_tx(conn, rule_code)

    def list_versions_in_tx(
        self, conn: sqlite3.Connection, rule_code: str | None = None
    ) -> list[RuleVersion]:
        if rule_code is None:
            rows = conn.execute(
                "SELECT * FROM versions ORDER BY rule_code, version"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM versions WHERE rule_code=? ORDER BY version",
                (rule_code,),
            ).fetchall()
        return [self._row_to_version(r) for r in rows]

    def list_published_candidates(self, conn: sqlite3.Connection) -> list[RuleVersion]:
        """已发布版本（含已撤回/已封存，历史日期仍可能适用）。"""
        rows = conn.execute(
            "SELECT * FROM versions WHERE status IN (?, ?)",
            ("已确认", "已封存"),
        ).fetchall()
        return [self._row_to_version(r) for r in rows]

    def insert_version(self, conn: sqlite3.Connection, v: RuleVersion) -> None:
        conn.execute(
            "INSERT INTO versions(version_id, rule_code, version, title, parameters,"
            " conditions, effective_start, effective_end, supersedes_version, status,"
            " draft_owner, signatures, published_at, withdrawn_at, withdraw_reason,"
            " sealed_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                v.version_id,
                v.rule_code,
                v.version,
                v.title,
                json.dumps(v.parameters, ensure_ascii=False),
                json.dumps(v.conditions, ensure_ascii=False),
                v.effective_start.isoformat(),
                v.effective_end.isoformat() if v.effective_end else None,
                v.supersedes_version,
                v.status,
                json.dumps(v.draft_owner.to_dict(), ensure_ascii=False)
                if v.draft_owner
                else None,
                json.dumps(v.signatures, ensure_ascii=False),
                v.published_at.isoformat() if v.published_at else None,
                v.withdrawn_at.isoformat() if v.withdrawn_at else None,
                v.withdraw_reason or None,
                v.sealed_at.isoformat() if v.sealed_at else None,
                v.created_at.isoformat() if v.created_at else None,
                v.updated_at.isoformat() if v.updated_at else None,
            ),
        )

    def update_version(self, conn: sqlite3.Connection, v: RuleVersion) -> None:
        conn.execute(
            "UPDATE versions SET title=?, parameters=?, conditions=?,"
            " effective_start=?, effective_end=?, supersedes_version=?, status=?,"
            " draft_owner=?, signatures=?, published_at=?, withdrawn_at=?,"
            " withdraw_reason=?, sealed_at=?, updated_at=? WHERE version_id=?",
            (
                v.title,
                json.dumps(v.parameters, ensure_ascii=False),
                json.dumps(v.conditions, ensure_ascii=False),
                v.effective_start.isoformat(),
                v.effective_end.isoformat() if v.effective_end else None,
                v.supersedes_version,
                v.status,
                json.dumps(v.draft_owner.to_dict(), ensure_ascii=False)
                if v.draft_owner
                else None,
                json.dumps(v.signatures, ensure_ascii=False),
                v.published_at.isoformat() if v.published_at else None,
                v.withdrawn_at.isoformat() if v.withdrawn_at else None,
                v.withdraw_reason or None,
                v.sealed_at.isoformat() if v.sealed_at else None,
                v.updated_at.isoformat() if v.updated_at else None,
                v.version_id,
            ),
        )

    # ---- events / accountings ----

    def add_event(
        self,
        conn: sqlite3.Connection,
        version_id: str,
        event: str,
        actor: Actor,
        at: datetime,
        detail: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_events(version_id, event, actor, at, detail)"
            " VALUES(?,?,?,?,?)",
            (
                version_id,
                event,
                json.dumps(actor.to_dict(), ensure_ascii=False),
                at.isoformat(),
                json.dumps(detail, ensure_ascii=False) if detail else None,
            ),
        )

    def list_events(self, version_id: str) -> list[dict[str, Any]]:
        with self.read_only() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE version_id=? ORDER BY seq",
                (version_id,),
            ).fetchall()
            result = []
            for row in rows:
                result.append(
                    {
                        "seq": row["seq"],
                        "event": row["event"],
                        "actor": json.loads(row["actor"]),
                        "at": row["at"],
                        "detail": json.loads(row["detail"]) if row["detail"] else None,
                    }
                )
            return result

    def insert_accounting(
        self, conn: sqlite3.Connection, record: dict[str, Any]
    ) -> None:
        conn.execute(
            "INSERT INTO accountings(accounting_id, period, vehicle, target_date,"
            " rule_code, version, version_id, parameters_snapshot, created_by,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                record["accounting_id"],
                record["period"],
                json.dumps(record["vehicle"], ensure_ascii=False),
                record["target_date"].isoformat(),
                record["rule_code"],
                record["version"],
                record["version_id"],
                json.dumps(record["parameters_snapshot"], ensure_ascii=False),
                json.dumps(record["created_by"].to_dict(), ensure_ascii=False),
                record["created_at"].isoformat(),
            ),
        )

    def list_accountings(self, period: str | None = None) -> list[dict[str, Any]]:
        with self.read_only() as conn:
            if period is None:
                rows = conn.execute(
                    "SELECT * FROM accountings ORDER BY period, accounting_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM accountings WHERE period=? ORDER BY accounting_id",
                    (period,),
                ).fetchall()
            return [
                {
                    "accounting_id": r["accounting_id"],
                    "period": r["period"],
                    "vehicle": json.loads(r["vehicle"]),
                    "target_date": r["target_date"],
                    "rule_code": r["rule_code"],
                    "version": r["version"],
                    "version_id": r["version_id"],
                    "parameters_snapshot": json.loads(r["parameters_snapshot"]),
                    "created_by": json.loads(r["created_by"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
