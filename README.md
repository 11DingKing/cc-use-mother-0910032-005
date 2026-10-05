# 车型基线规则库

本项目维护车型基线规则库的领域约定、角色边界与后端实现，供后端服务、接口和自动化验证统一使用。覆盖企业申报员、核算专员、交易运营员、监管审计员四类角色，并落实**规则生效区间、条件冲突检测、历史适用冻结、并发唯一发布**四项关键不变量。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/baseline_rules/`：规则库后端。
  - `models.py`：不可变领域模型（`RuleVersion` / `Draft` / `Withdrawal` / `Event` / `Resolution`）。
  - `conflict.py`：车型条件的可满足性、互斥与覆盖等价判定。
  - `service.py`：发布门禁、撤回、紧急勘误、未来预告、按日期解析与快照归档。
  - `api.py`：标准库实现的 HTTP API（`build_server` / `serve`）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约与后端回归测试。

## 核心语义

1. **版本只追加、不可变**。每次发布生成带内容签名（SHA-256 前缀）的 `规则ID#vN`；撤回是独立签署记录，勘误是指向旧版的新版本（`supersedes`），预告是 `kind=preview` 的占位版本。历史核算引用的版本与参数永不被改写。
2. **禁止追溯**。常规发布与勘误的生效起始日不得早于签署日；撤回同样不得追溯。勘误只能向前治理，历史日期仍解析到原版本。
3. **发布前成对检测**。草稿按效力切换点（区间端点、撤回日）切段，与每个当日治理版本比对：**区间重叠且适用条件能被同一车型同时满足**才拒绝（否则未来解析不唯一）；条件互斥的规则（如纯电/混动分治）允许同期共存；显式 `supersedes` 的重叠为预期，但替代版本必须与被替代版本覆盖**完全相同的车型集合**（勘误只改参数）。
4. **按日期+车型唯一解析**。依次过筛：生效窗口（闭区间）→ 撤回截断 → 预告排除 → 适用条件（合取）→ 替代链遮蔽；输出 `unique` / `none` / `ambiguous` 与逐版本、逐步骤的 `match_path` 解释。
5. **并发唯一发布**。服务以全局锁把"检测→写入"原子化，并发竞争的发布恰有一个成功，其余收到 409 冲突明细。
6. **审计与归档**。所有发布/撤回进入只追加事件链；`snapshot` 可导出全部不可变记录，恢复时校验内容签名，防篡改。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/precheck` | 仅做区间重叠/条件冲突检测，返回 `conflicts` 与允许共存清单 |
| POST | `/v1/publish` | 发布（`kind=regular|errata|preview`，勘误/承接带 `supersedes`） |
| POST | `/v1/withdraw` | 签署撤回（可指定未来的撤回生效日） |
| GET  | `/v1/resolve?date=YYYY-MM-DD&vehicle=<JSON>` | 解析唯一规则并返回匹配路径 |
| GET  | `/v1/versions[?rule_id=]` | 已发布版本（不可变） |
| GET  | `/v1/versions/{version_id}` | 单个版本 |
| GET  | `/v1/withdrawals/{version_id}` | 撤回签署记录 |
| GET  | `/v1/events` | 追加事件审计链 |
| GET  | `/v1/snapshot` | 完整归档快照 |

车型档案为 JSON 对象，条件字段支持 `energy_type`、`category`、`fuel_label`、`weight_class`（数值，支持 `gte/lte`）、`powertrain`、`model_code`，运算符 `eq` / `in`。

启动服务：

```bash
PYTHONPATH=src python3 -m baseline_rules.api   # 默认 127.0.0.1:8000
# 或安装后：baseline-rules-api
```

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
