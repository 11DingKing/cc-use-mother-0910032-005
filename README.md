# 车型基线规则库

本项目维护车型低能耗基线规则库的领域约定、角色边界与后端实现，供企业申报、核算复核、政策发布与监管审计统一使用。

## 领域约束

- **规则生效区间**：每个版本携带 `[effective_start, effective_end)` 半开区间，开放终点表示长期有效。
- **条件冲突检测**：发布前检测区间重叠；仅当重叠区间内两组车型条件*可能同时命中同一车型*时才判冲突，互斥条件（如纯电/插混）允许并行。
- **历史适用冻结**：已发布版本内容不可修改；撤回、紧急勘误、未来版本预告都只追加新版本，不改写参数与区间。撤回/封存仅影响当时点之后，历史日期永远解析到当时有效的版本与参数。年度申报核算以只增快照存档。
- **并发唯一发布**：所有写操作在 SQLite `BEGIN IMMEDIATE` 事务内“先重读、再校验、后写入”，库级写锁串行化并发发布，冲突检测不会漏判；若底层数据被破坏导致同日同车多版本命中，解析器显式报错而非静默选择。

## 角色与生命周期

| 角色 | 权限 |
| --- | --- |
| 企业申报员 | 创建/修改草稿、提交核算、登记未来版本预告、登记核算 |
| 核算专员 | 退回修订、发布签署、登记核算 |
| 交易运营员 | 查询解析（只读） |
| 监管审计员 | 发布签署、发布、撤回、紧急勘误、封存 |

版本状态：`草稿 → 待核算 → 已确认 →（生效区间内动态呈现“执行中”）→ 撤回后可封存为 已封存`。
发布必须集齐核算专员与监管审计员两方签署，且生效起点不得早于发布当日（禁止追溯）。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/baseline/`：规则库后端
  - `model.py`：角色、版本模型、区间运算、生命周期状态
  - `conditions.py`：车型条件引擎（匹配、自冲突、跨版本相容性）
  - `repository.py`：SQLite 仓储（WAL + 立即事务）
  - `service.py`：生命周期、签署、冲突检测、撤回/勘误/预告、核算快照
  - `resolver.py`：按日期+车型解析唯一规则并输出匹配路径
  - `api.py`：零依赖 HTTP API（标准库）
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约、领域、并发与 HTTP 端到端测试。

## HTTP API 摘要

写操作在请求体携带 `actor: {actor_id, name, role}`。

| 方法与路径 | 说明 |
| --- | --- |
| `POST /api/rules/drafts` | 创建草稿（可带 `supersedes_version`） |
| `POST /api/rules/announce` | 未来版本预告（生效起点须晚于今天） |
| `GET  /api/rules` / `GET /api/rules/{code}/versions` | 查询 |
| `PATCH /api/versions/{id}` | 修改草稿（仅草稿态） |
| `POST /api/versions/{id}/submit` `/return` `/sign` `/publish` | 提交/退回/签署/发布 |
| `POST /api/versions/{id}/withdraw` `/seal` `/emergency-correction` | 撤回/封存/紧急勘误 |
| `GET  /api/versions/{id}` `/history` | 版本详情与审计事件流 |
| `POST /api/resolve` | 按 `date` + `vehicle` 解析唯一规则，返回参数与 `match_path` |
| `POST /api/accountings` `GET /api/accountings?period=` | 核算快照登记与查询（只增） |

`/api/resolve` 的 `match_path` 依次列出状态过滤、区间过滤、条件合取匹配、替代遮蔽四个阶段对每个版本的淘汰原因或命中结论。

## 验证

```bash
# 测试（领域 + 并发 + HTTP 端到端）
python3 -m unittest discover -s tests -v

# 编译
python3 -m compileall -q src tools tests

# 契约检查
python3 tools/check_contract.py domain/contract.json

# 启动 API
PYTHONPATH=src python3 -m baseline.api --host 127.0.0.1 --port 8080 --db baseline.db
```
