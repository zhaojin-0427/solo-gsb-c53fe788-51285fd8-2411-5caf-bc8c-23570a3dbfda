# 实验室配液计划与偏差追溯 API

基于 **Python + FastAPI + PostgreSQL + OR-Tools CP-SAT** 的不可变配液计划服务。它根据批次库存、效期、目标浓度区间、容器容量、移液枪最小刻度与污染/禁配顺序，生成整数体积的多步混配计划；执行中录入实测偏差后，只重算受影响的下游分装，并返回完整来源链。批次质量维度上，服务以不可变检测记录驱动“放行/隔离/召回”状态机，计划、revision 与提交全程校验已放行批次，召回时同事务冻结受影响预占并追溯受影响的计划、分装与来源边。

## 核心能力

- 批次库存、浓度、可用体积、效期管理。
- 目标浓度闭区间约束，全部移液体积均为正整数且满足最小体积/刻度倍数。
- 可选“原液 → 均质中间液 → 最终分装”，以及“原液直接混合到最终容器”两类路径。
- 支持批次禁配（不能进入同一混配容器）与污染先后顺序。
- 按以下顺序确定性择优：
  1. 废料体积最少；
  2. 移液步骤数最少；
  3. 批次顺序分最优（早效期、稳定字典序优先；固定 CP-SAT seed 和单线程搜索）。
- 不可变 revision：每次求解生成新 revision，已提交 revision 只能被 supersede，不会被覆盖。
- 幂等键两阶段安全：质量登记、召回、创建计划及 revision、提交 revision、记录偏差等所有状态变更接口，业务变更与最终状态码、完整响应体在**同一事务**原子落库；同键同体请求等待首请求完成并重放其最终响应，同键异体请求返回 `409`，失败或进程中断不会残留任何可重放占位。
- 幂等键 + 库存行锁 + 版本 CAS 原子提交预占。
- 批次质量状态机：`pending → released / quarantined`，`released → recalled`；检测记录不可变，所有状态迁移用 `expected_version` CAS 原子追加事件。
- 创建计划、新 revision 与提交预占三级校验“当前有效且已放行”，被阻断时返回阻断批次及其最近事件。
- 召回已放行批次时，在同一事务冻结受影响已提交 revision 的全部未执行预占，并返回受影响的计划、分装与来源边；接口支持幂等重放。
- 无解时自动最小松弛，返回最少冲突类型、证据和一个 witness 方案；冲突 revision 禁止提交。
- 偏差录入后只重算受影响的 aliquot，返回当前完整来源链和被替代的边。

## 一键启动

前置条件：已安装 Docker 与 Docker Compose v2（`docker compose version` 可输出版本）。

```bash
docker compose up --build
```

该命令会依次：

1. 启动 PostgreSQL 16（带 `pg_isready` 健康检查，API 等数据库就绪后再启动）；
2. 构建并启动 API 容器；
3. API 启动时自动执行 `CREATE TABLE`，并对旧版本 `idempotent_operations` 表做轻量在线升级（详见下文[“旧库启动迁移”](#旧库启动迁移)；生产建议改用 Alembic）。

服务启动后访问：

- API 服务（根路径返回 Swagger 重定向/可经由 `/docs` 使用）：<http://localhost:8000>
- Swagger UI：<http://localhost:8000/docs>
- OpenAPI JSON：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/health>（返回 `{"status":"ok"}`）

停止并保留数据库：

```bash
docker compose down
```

停止并删除数据库卷（清空全部数据后重新初始化）：

```bash
docker compose down -v
```

如需调整幂等并发参数，可在启动时注入（带默认值）：

```bash
IDEMPOTENT_LOCK_TIMEOUT_SECONDS=30 IDEMPOTENT_STALE_TIMEOUT_SECONDS=120 docker compose up --build
```

## 旧库启动迁移

服务每次启动时都会检查 `idempotent_operations` 表并做幂等的轻量迁移（重复执行结果不变，对全新数据库为空操作）：

- **补齐状态机列**：为旧版（两阶段实现）表添加 `state` / `locked_at` / `completed_at` 列；旧表中已完成的记录标记为 `completed`，`completed_at` 回填为 `created_at`，并放开 `status_code` / `response_json` 的 `NOT NULL` 约束以容纳进行中的 `in_progress` 行。
- **清理不完整占位行**：旧两阶段实现在业务变更**之前**先提交一行 `status_code=200, response_json={}` 的占位记录，事后再改写为真实结果；若进程在两次提交之间崩溃，该占位会持久残留。启动迁移会识别并删除这类没有最终结果的占位行（响应体为空对象，或缺少最终状态码/响应体的记录），**只保留含完整最终状态码和可序列化响应体的合法完成记录**。被清理的幂等键在下次请求时会重新真实执行，而不是重放空的成功响应；合法完成记录继续原样重放。
- **修复已被误标的库**：曾被旧版迁移错误标记为 `completed` 的占位行，会在每次启动时继续被识别并清除；处于 `in_progress` 的行（进行中的请求）不受影响。
- 迁移会打印清理的占位行数量（WARNING 日志），便于运维核对哪些幂等键需要客户端重试。

## 运行测试

```bash
pip install -r requirements.txt
pytest tests/
```

默认使用临时 SQLite 库运行。另有两个针对真实 PostgreSQL 的测试（并发幂等序列化、旧表启动迁移），默认跳过；将 `DATABASE_URL` 指向测试用 PostgreSQL 并设置 `RUN_PG_IDEMPOTENCY_TESTS=1` 即可执行：

```bash
DATABASE_URL='postgresql+psycopg2://lab:lab@localhost:5432/lab_planning' \
RUN_PG_IDEMPOTENCY_TESTS=1 pytest tests/
```

## 快速演示

### 1. 创建两个库存批次

```bash
curl -s -X POST http://localhost:8000/stock-lots \
  -H 'Content-Type: application/json' \
  -d '{
    "code":"HIGH-2026",
    "concentration":"2.0",
    "available_volume":10000,
    "expires_at":"2026-12-31T00:00:00Z"
  }'
```

```bash
curl -s -X POST http://localhost:8000/stock-lots \
  -H 'Content-Type: application/json' \
  -d '{
    "code":"LOW-2026",
    "concentration":"0.0",
    "available_volume":10000,
    "expires_at":"2026-10-31T00:00:00Z"
  }'
```

记录返回的 `id`，下面用 `$HIGH_ID`、`$LOW_ID` 表示。新批次初始质量状态为 `pending`，必须登记检测记录并放行后才能被计划引用。

### 2. 登记检测记录并放行批次

为每个批次登记一条不可变检测记录（项目、数值、单位、上下限、规则版本），并用 `expected_version` 做 CAS。检测值落在闭区间 `[lower_limit, upper_limit]` 内 → 批次转为 `released`；否则转为 `quarantined`。

```bash
curl -s -X POST http://localhost:8000/stock-lots/$HIGH_ID/qc-records \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: qc-high-1' \
  -d '{
    "item":"assay",
    "value":"99.5",
    "unit":"%",
    "lower_limit":"95",
    "upper_limit":"105",
    "rule_version":"QC-SOP-1.0",
    "expected_version":1
  }'
```

对 `$LOW_ID` 执行同样操作。返回中 `lot.quality_status` 变为 `released`、`lot.version` 递增，同时追加一条 `released` 事件；可用 `GET /stock-lots/{id}/qc-records` 查询不可变检测记录、`GET /stock-lots/{id}/events` 查询完整状态事件流。

### 3. 创建计划

请求 3 支 1000 μL、目标浓度 `0.8~1.2` 的最终分装；移液最小 100 μL、刻度 50 μL。

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: plan-demo-1' \
  -d "{
    \"name\":\"demo\",
    \"target_min_concentration\":\"0.8\",
    \"target_max_concentration\":\"1.2\",
    \"aliquot_volume\":1000,
    \"aliquot_count\":3,
    \"final_container_capacity\":1500,
    \"intermediate_container_capacity\":4000,
    \"max_intermediate_containers\":2,
    \"pipette_min_volume\":100,
    \"pipette_increment\":50,
    \"scheduled_at\":\"2026-10-01T00:00:00Z\",
    \"stock_lot_ids\":[\"$HIGH_ID\",\"$LOW_ID\"],
    \"forbidden_pairs\":[],
    \"contamination_orders\":[]
  }"
```

返回 `revision.id`（记为 `$REV_ID`）。`solution.feasible=true` 时，可查看：

- `transfers`：带全局步骤号的移液操作；
- `intermediates`：中间液输入、浓度、废料；
- `aliquots`：每支最终分装的来源路径；
- `source_chain`：完整节点/边来源链；
- `score`：废料、步骤数、批次顺序分。

### 4. 幂等提交库存预占

```bash
curl -s -X POST http://localhost:8000/revisions/$REV_ID/commit \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: commit-demo-1' \
  -d '{
    "expected_revision_version":1,
    "expected_lot_versions":{},
    "force":false
  }'
```

若客户端已读取库存版本，可传 `expected_lot_versions`；版本变化会返回 `409 lot_version_conflict`。重复使用同一个 `Idempotency-Key` 和相同请求体会返回同一结果，不会重复预占。提交时会再次校验所有相关批次仍为 `released`，否则返回 `409 lot_not_released` 并附阻断批次及其最近事件。

### 5. 录入中间液实测偏差

只有计划中存在 `intermediate-x` 时适用。假设中间液计划 3000 μL，实测只剩 2500 μL：

```bash
curl -s -X POST http://localhost:8000/revisions/$REV_ID/deviations \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: deviation-demo-1' \
  -d '{
    "intermediate_id":"intermediate-1",
    "measured_volume":2500,
    "measured_concentration":"1.0"
  }'
```

返回：

- `affected_aliquot_ids`：受影响的下游分装；
- `result.replacement_plan.transfers`：仅针对受影响分装的追加/替代操作；
- `result.source_chain`：完整当前来源链，被替代的原边标记为 `"status":"superseded"`；
- `result.feasible=false` 时，`result.conflicts` 给出最小冲突证据，不产生新增库存预占。

### 6. 召回已放行批次

当某批次需要召回时，先 `GET /stock-lots/$HIGH_ID` 确认当前 `version`，然后：

```bash
curl -s -X POST http://localhost:8000/stock-lots/$HIGH_ID/recall \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: recall-demo-1' \
  -d '{
    "expected_version":3,
    "reason":"supplier notification"
  }'
```

同一事务内完成：批次状态转为 `recalled` 并追加召回事件；所有引用该批次的**已提交 revision** 的未执行预占（`active`/`deviation`）被冻结为 `frozen`。返回：

- `event`：追加的 `recalled` 事件（含 `version_after`）；
- `affected_plans`：受影响的计划与 revision、`frozen_volume`、`frozen_reservation_ids`；
- `affected_aliquot_ids` / `affected_edges`：从该批次沿来源链可达的分装与来源边。

只有 `released` 状态的批次可召回（否则 `409 lot_not_recallable`）；`expected_version` 不匹配返回 `409 lot_version_conflict`；相同 `Idempotency-Key` + 相同请求体重放返回首次记录的结果，不会重复冻结或追加事件。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/stock-lots` | 创建批次库存 |
| `GET` | `/stock-lots` | 查询库存批次 |
| `GET` | `/stock-lots/{id}` | 查询单个批次 |
| `POST` | `/stock-lots/{id}/qc-records` | 登记不可变检测记录（`expected_version` CAS），值在界限内放行、否则隔离 |
| `GET` | `/stock-lots/{id}/qc-records` | 查询批次的不可变检测记录 |
| `GET` | `/stock-lots/{id}/events` | 查询批次质量状态事件流 |
| `POST` | `/stock-lots/{id}/recall` | 召回已放行批次，同事务冻结受影响已提交 revision 的未执行预占 |
| `POST` | `/plans` | 创建计划和 revision 1，支持幂等键 |
| `GET` | `/plans/{id}` | 查询计划及全部不可变 revisions |
| `POST` | `/plans/{id}/revisions` | 基于最新请求重算或提交新请求，生成新 revision |
| `GET` | `/revisions/{id}` | 查询 revision 完整方案、冲突和来源链 |
| `POST` | `/revisions/{id}/commit` | CAS + 幂等原子提交库存预占 |
| `POST` | `/revisions/{id}/deviations` | 录入实测偏差，重算受影响下游分装 |

所有会改变状态的关键接口都支持请求头：

```http
Idempotency-Key: unique-key-for-this-operation
```

相同键 + 相同请求体重放首次的完整状态码和响应体（不重复执行）；相同键 + 不同请求返回 `409 idempotency_conflict`；首请求进行中的并发同键请求等待其完成，超时则返回 `409 idempotency_in_progress`（带 `Retry-After`，用原键原体重试）。完整语义见下文[“幂等语义（两阶段提交安全）”](#幂等语义两阶段提交安全)。

## 无解与最小冲突

CP-SAT 在普通硬约束模型无可行解时，会用布尔松弛变量重解并最小化松弛数量。典型冲突类型：

- `unmet_aliquot`：某支最终分装无法满足；
- `inventory`：批次未预留体积不足；
- `concentration`：浓度区间无法满足；
- `forbidden_pair`：禁配批次必须同容器；
- `contamination_order`：污染先后顺序无法满足；
- `intermediate_container_capacity`：中间容器容量不足。

每条冲突都带证据，例如批次 ID、容器 ID、实际浓度、实际体积、参与批次等。API 返回 HTTP `201` 创建 revision，但 `feasible=false`；调用 `/commit` 会得到 `409 infeasible_revision`。

效期是硬前置条件：列入 `stock_lot_ids` 的批次若在 `scheduled_at` 已到期，直接返回 HTTP `422 expired_lot`，因为该批次从时刻上已不可作为候选。

## 批次质量放行与召回

- 批次质量状态机：`pending`（新建）→ `released` / `quarantined`（登记检测记录后按界限判定）→ `recalled`（仅 `released` 可召回）。
- 检测记录（`qc_records`）不可变：项目、数值、单位、上下限、规则版本与判定结果一旦写入不可修改；每个 `pending` 批次只接受一条检测记录（重复登记返回 `409 lot_not_testable`）。
- 所有状态迁移都要求请求体携带 `expected_version`，服务端先 `SELECT ... FOR UPDATE` 锁定批次行、校验版本，再在同一事务内更新状态、`version += 1` 并追加一条 `lot_events` 事件（`(lot_id, version_after)` 唯一约束兜底保证原子追加）；版本不匹配返回 `409 lot_version_conflict` 及 `current_version`。
- 创建计划与生成新 revision 时，`stock_lot_ids` 中任何非 `released` 批次都会以 `422 lot_not_released` 阻断；提交预占时再次校验，以 `409 lot_not_released` 阻断。两者的 `details.conflicts[0].evidence` 都列出阻断批次的当前质量状态与最近一条事件。
- 召回在同一事务内：批次转 `recalled`、追加召回事件、把受影响已提交 revision 的全部未执行预占（`active`/`deviation`）冻结为 `frozen`，并沿 revision 的不可变来源链计算受影响的分装与来源边一并返回。

## 幂等语义（两阶段提交安全）

下列所有状态变更接口都接受请求头 `Idempotency-Key: <客户端生成的唯一键>`：

- `POST /stock-lots/{id}/qc-records`（质量登记/放行/隔离）
- `POST /stock-lots/{id}/recall`（召回）
- `POST /plans`（创建计划及 revision 1）
- `POST /plans/{id}/revisions`（创建 revision）
- `POST /revisions/{id}/commit`（提交 revision、原子预占）
- `POST /revisions/{id}/deviations`（记录偏差）

幂等记录保存在 `idempotent_operations` 表，每行经历：

```text
in_progress ──成功──▶ completed（与业务变更在同一事务提交）
     │
     └──失败 / 进程中断──▶ 随事务回滚，数据库中不残留任何该行
```

### 原子性：业务变更与响应一次提交

处理一个带键请求时，服务端在**单个数据库事务**内完成：

1. 以键的哈希获取 PostgreSQL 事务级咨询锁 `pg_advisory_xact_lock`（同键串行化）；
2. 写入 `state='in_progress'` 标记（仅在本事务内可见），并执行全部业务变更（行锁 + CAS、追加事件/预占/偏差等），只 `flush` 不提交；
3. 构造最终响应后，把幂等行更新为 `state='completed'` 并写入**最终 HTTP 状态码与完整可序列化响应体**；
4. 执行**唯一一次 `COMMIT`**——业务数据与可重放结果同时可见。

因此：

- 进程在响应写回客户端前任意时刻崩溃，事务整体回滚，业务变更与幂等占位都不会落库；客户端用同一个键重试即重新执行，绝不会拿到“`200` + 空对象”之类的伪成功结果；
- 不存在“业务先提交、幂等结果后提交”的时间窗，也就没有占位被并发请求读到的可能。

### 请求重放与冲突

| 场景 | 行为 |
| --- | --- |
| 相同键 + 相同操作 + 相同请求体 | 首请求完成后，后续请求**直接重放**首次存储的状态码与响应体，不重复执行业务（不重复放行/冻结/预占/追加 revision 或偏差） |
| 相同键 + 相同请求体并发到达 | 后来者在咨询锁上等待首请求完成（默认最多 30s），随后重放最终响应；两边得到完全一致的结果 |
| 相同键 + 不同操作类型或不同请求体 | 返回 `409 idempotency_conflict`，即使首请求仍在处理中也立即冲突 |
| 首请求仍在处理、同键同体等待超时 | 返回 `409 idempotency_in_progress`（带 `Retry-After: 2`），客户端用**同一键、同一体**稍后重试即可 |
| 业务校验失败（CAS 不匹配、状态不允许等） | 返回对应错误（如 `409 lot_version_conflict`），事务回滚，**幂等表不保留任何行**；同一键可立即用于修正后的请求 |

“相同请求体”通过对操作类型、路径参数与 JSON 请求体做规范化 SHA-256 哈希比较，而不是逐字节比较。

### 崩溃后的陈旧标记

正常情况下 `in_progress` 行只存在于未提交事务中，进程死亡即随回滚消失。若因极端情况遗留了已提交的 `in_progress` 标记（例如旧版本数据、手工干预），其 `locked_at` 超过 `IDEMPOTENT_STALE_TIMEOUT_SECONDS`（默认 120 秒）后，新的同键同体请求会接管该键并重新执行；未超过阈值时返回 `409 idempotency_in_progress` 提示重试。

### 不带键的请求

未携带 `Idempotency-Key` 的请求照常处理，但不享有重放保护——网络超时时是否重试由调用方自行承担风险。

## 数据模型与并发说明

- `stock_lots` 保存库存、预占量、质量状态 `quality_status` 与单调递增 `version`。
- `qc_records` 保存不可变检测记录（项目/数值/单位/上下限/规则版本/判定）。
- `lot_events` 保存批次质量状态事件流，`version_after` 记录迁移后的批次版本。
- `plan_revisions` 保存不可变请求和求解结果，状态可为 `draft/committed/superseded`。
- `reservations` 保存每个已提交 revision 对批次的预占，状态可为 `active/deviation/superseded/frozen`。
- `idempotent_operations` 以幂等键为主键，保存操作类型、请求体哈希、`state`（`in_progress/completed`）、最终 `status_code`、完整 `response_json`、`locked_at`/`completed_at`；只有 `completed` 行可被重放。
- 提交时 PostgreSQL `SELECT ... FOR UPDATE` 锁定相关批次行；同一事务内释放旧 revision、创建新 reservation、更新版本。
- 同键并发由键哈希上的事务级咨询锁串行化；锁在 `COMMIT`/`ROLLBACK` 时由 PostgreSQL 自动释放（包括 worker 进程被杀死）。
- 新版本提交成功后，旧 committed revision 变为 `superseded`，旧 reservations 同步标记为 `superseded`。

## 本地开发（不使用 Docker）

需要 Python 3.11、PostgreSQL 16，并安装依赖：

```bash
pip install -r requirements.txt
export DATABASE_URL='postgresql+psycopg2://lab:lab@localhost:5432/lab_planning'
uvicorn app.main:app --reload
```

应用启动时会自动执行 `CREATE TABLE`。生产环境建议再引入 Alembic 迁移。

## 目录结构

```text
app/
  main.py       # FastAPI 路由与错误响应
  models.py     # SQLAlchemy 数据模型
  schemas.py    # Pydantic 请求/响应模型
  solver.py     # OR-Tools CP-SAT 建模、松弛冲突和来源链构造
  services.py   # revision、幂等编排（咨询锁 + in_progress/completed 原子提交）、CAS 提交、偏差重算、质量放行与召回
  database.py   # 数据库引擎/session/建表
docker-compose.yml
Dockerfile
requirements.txt
```
