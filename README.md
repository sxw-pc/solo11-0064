# 漏斗验算台

**面向本地事件日志的声明式漏斗/留存计算核验台。** 把“用户行为序列怎么算、边界怎么定、结果为什么变了”变成可复现、可解释、可审计的本地记录。

它是一条完全离线的批处理流水线：读取本地 JSONL 事件与 YAML 分析规格，使用同一个确定性引擎计算漏斗、留存、分群与数据质量，输出 JSON、Markdown、自包含 HTML、运行清单和 SQLite 台账；还支持对比两次运行，区分**输入数据变化**与**声明式计算规格变化**。

## 快速开始

要求 Python 3.11+。依赖安装完成后，运行期不需要网络。

```sh
python -m pip install -r requirements.txt

# 1. 跑内置合成 fixture（3 次运行 + 2 次对比）
python -m funnelcheck demo

# 2. 查看运行台账（只绑定回环地址）
python -m funnelcheck serve --host 127.0.0.1 --port 8765

# 3. 跑全部测试
python -m pytest -q
```

打开 `http://127.0.0.1:8765/` 查看台账。服务和自包含报告不会请求 CDN、外部字体、远程 API、遥测或任何外部资源。

也可以对本地文件运行：

```sh
python -m funnelcheck run \
  --events path/to/events.jsonl \
  --spec path/to/spec.yaml \
  --runs-dir runs

python -m funnelcheck compare \
  --baseline run-e589cf333d4c3e313e72 \
  --candidate run-96ae762ce67d8e2a732c \
  --runs-dir runs

python -m funnelcheck list --runs-dir runs
```

`demo` 会基于同一份事件生成两次对比：

- **规格变化**：`late_policy`、漏斗窗口和留存周期发生变化，判定为 `config_changed`；
- **输入变化**：规格不变，候选事件文件移除两个事件，判定为 `input_changed`。

## 边界

它**是**：

- 纯本地批处理 + 只读本地 Web 的漏斗/留存核验工具；
- 把窗口、同刻顺序、去重、迟到/未来事件、时区、留存周期写成 YAML 的计算器；
- 对重复、乱序、迟到、无效时间、未来事件做质量标记的审计工具；
- 输出 JSON、Markdown、自包含 HTML、SQLite 台账与运行清单的确定性流水线；
- 支持 baseline/candidate 对比并解释差异来源的回归工具。

它**不是**：

- 埋点 SDK、实时流处理、消息队列或分布式计算平台；
- 指标口径字典、口径校对器、A/B 平台或因果归因系统；
- 通用 BI、SQL 查询引擎、可视化大屏；
- 身份图谱、跨设备 ID 缝合、机器学习推荐；
- 数据脱敏、抽样准入、血缘抽取、湖仓版本管理、采集爬虫或安全扫描器；
- 多租户/登录/RBAC/云上传/外部数据库连接/任何联网调用。

它只回答：“按这份事件和这份规格，漏斗与留存应是多少；边界如何处理；两次运行为何不同”。它不回答“口径是否应该这样定义”。

## 目录

```text
funnelcheck/
  cli.py                 命令入口（demo/run/compare/list/serve）
  spec.py                Pydantic 规格模型、YAML 严格加载
  loader.py              JSONL 加载与 20MB 限制
  normalize.py           时间、去重、策略、数据质量
  engine/
    dedup.py             去重键与保留策略
    quality.py           质量计数与样例
    funnel.py            有序漏斗与分群
    retention.py         日/周队列留存与分群
    compare.py           指标/规格/输入差异与归因
    order.py             唯一事件排序规则
  report.py              报告组装、稳定 JSON、原子写入
  render_markdown.py     Markdown 报告
  render_html.py         自包含 HTML 报告
  ledger.py              SQLite 运行台账
  web.py                 本地只读 FastAPI 查看器
  templates/             服务端 Jinja2 页面
  static/app.css         本地样式（无 CDN/字体）
  fixtures/              全合成示例事件与规格
tests/                   单元、集成、Web、CLI 测试
```

## 分析规格

完整示例见 `funnelcheck/fixtures/spec.yaml`。

```yaml
spec_version: 1
title: 订阅转化与回访留存（基线）
analysis:
  timezone: UTC
  as_of: "2026-03-16T00:00:00Z"
  future_grace: 1h
  late_after: 48h
  late_policy: exclude      # exclude | include
events:
  dedup_key: event_id      # event_id | content
  dedup_keep: first_received  # first_received | last_received | first_line
  same_timestamp_order: received_then_id  # 或 id_then_received
funnel:
  name: signup_to_purchase
  window: 7d
  steps:
    - name: 浏览落地页
      event: landing_view
    - name: 完成注册
      event: signup
      where:
        properties.plan: placeholder-free  # 可选，标量精确匹配
    - name: 完成购买
      event: purchase
retention:
  cohort_event: signup
  return_event: landing_view
  unit: day                 # day | week
  periods: [1, 3, 7]
  mode: exact               # exact | cumulative
segments:
  - field: channel
  - field: region
```

字段路径支持根字段（如 `channel`）和 `properties.*`。未知字段在配置中是错误，不会被静默忽略。

## 计算语义（可审计规则）

### 事件输入与质量

- 输入为非空 JSON 对象逐行组成的 UTF-8 JSONL；默认单文件上限 **20 MiB**，超限返回 `FC_INPUT_TOO_LARGE`，不崩溃、不抽样。
- 必填字段：`event_id`、`user_id`、`event_name`、`occurred_at`、`received_at`。
- 无时区偏移的时间按 `analysis.timezone` 解释。夏令时歧义时间取较早偏移（`fold=0`）并标记；不存在的本地时间视为无效时间。
- 未来规则：`occurred_at > as_of + future_grace` 排除；`as_of < occurred_at <= as_of+future_grace` 计数但纳入。留存观察截止取 `as_of + future_grace` 的本地日期。
- 迟到规则：`received_at - occurred_at > late_after` 计为迟到；`late_policy` 决定纳入或排除。接收时间早于发生时间单独计数。
- 乱序：按输入行顺序，同一用户的 `occurred_at` 比该用户此前最大发生时间更早，计一次乱序样例。
- 所有规则与计数写入 `quality` 和报告 Markdown/HTML。

### 去重

- `event_id`：同 ID 视为重复。
- `content`：`user_id + event_name + occurred_at + received_at + attributes` 完全相同视为重复。
- 保留策略：`first_received`（接收时间最早，平局取先出现行）、`last_received`（接收时间最晚）、`first_line`（首行）。
- 去重后记录数、重复行数和重复组数写入质量台账。

### 唯一排序

同一用户事件总按 `occurred_at` 升序；同刻事件按规格二选一：

- `received_then_id`：`received_at`，再 `event_id`；
- `id_then_received`：`event_id`，再 `received_at`。

行号只在所有字段相同的最终平局中作为稳定兜底。漏斗和留存共用该规则。

### 漏斗

- 用户按上述顺序扫描事件；步骤必须按声明顺序匹配，每个步骤消耗一个匹配事件。
- 以**首个命中步骤 1 的事件**为入口；后续步骤必须在其 `funnel.window` 内完成（含端点），否则该用户不转化，不重新尝试入口。
- 允许跳过无关事件；`where` 对事件字段做标量精确匹配（字符串化后比较）。
- 输出每步用户数、较首步转化率、较上一步转化率和上一步流失。
- 分群值取用户**命中步骤 1 的事件**对应字段；缺失值为 `(missing)`。

### 留存

- 每个用户首次命中 `cohort_event` 作为队列事件；按分析时区的**本地日历日期**归入 Cohort。
- 回访事件按本地日期差归入周期；第 1 天/第 1 周为下一个完整日/周，不把 Cohort 当天算作回访。
- `exact` 只统计恰好第 N 周期；`cumulative` 统计 1..N 周期内至少一次回访。
- 只有完整结束的周期才进入分母（周期结束后次日）。分母为 `eligible_users`，留存率为 `retained_users / eligible_users`；没有可观察用户时为 `null`。
- 输出总览、每日 Cohort 明细和分群；留存分群取 Cohort 事件字段。

### 确定性与舍入

- 所有比率保留 6 位小数，按普通四舍五入（Python `round`，银行家舍入）输出；比较差异也保留 6 位。
- 报告 JSON 使用 `sort_keys=True`、固定缩进、UTF-8，无生成时间，因而同一输入和规格会生成字节一致的核心报告。
- `run_id = "run-" + sha256(语义版本 | 规格规范化哈希 | 原始事件文件哈希)[:20]`。
- 台账 `created_at_utc` 只存在于 SQLite 台账（审计写入时间），不污染确定性产物。
- 报告不含任何用户 ID 明细；只保留聚合计数和事件 ID 级质量样例。

## 产物

一次运行生成：

```text
runs/
  ledger.sqlite
  run-<id>/
    report.json      # 稳定键排序的完整报告
    report.md        # 可审阅的 Markdown
    report.html      # 完全自包含（内联 CSS、无脚本/外部资源）
    manifest.json    # 版本、输入/规格指纹、产物哈希
  comparisons/
    cmp-<id>/
      compare.json
      compare.md
      compare.html
      manifest.json
```

对比判定：

| verdict | 含义 |
| --- | --- |
| `identical` | 输入、规格、引擎与指标均一致 |
| `no_metric_change` | 输入/规格有变化，但核心指标无变化 |
| `config_changed` | 输入一致，指标变化可归因于分析规格 |
| `input_changed` | 规格一致，指标变化可归因于输入事件 |
| `config_and_input_changed` | 两者同时变化，本次对比无法隔离单一原因 |
| `engine_changed` | 引擎/语义版本变化且指标变化 |
| `unexplained_metric_change` | 输入、规格、引擎均不变但指标不同 |

对比只做**差异归因**，不做因果推断或显著性检验。规格差异会逐路径列出；输入差异用原始文件 SHA-256 与规范化 SHA-256 双重指纹表示。

## 本地只读 Web

- 默认且仅允许绑定 `127.0.0.1`、`::1` 或 `localhost`；`0.0.0.0` 等地址会直接拒绝。
- 只有读取页面与 API，没有写入、上传、删除接口。
- 页面数据来自本地 SQLite 台账和运行目录；路径经过 ID 格式与根目录边界校验。
- 无外部脚本、字体、图表库；漏斗/留存图形由服务端 HTML/CSS 条形呈现。
- API：`/api/runs`、`/api/runs/{run_id}`、`/api/comparisons`、`/api/comparisons/{id}`、`/healthz`。

## 测试

```sh
python -m pytest -q
```

测试覆盖规格校验、20MB 限制、去重与同刻排序、DST/时区、漏斗窗口、`where`、数据质量、日/周留存、对比归因、报告确定性、台账、Web 回环与只读页面、CLI 完整流程。全部使用 `tmp_path`，不会向仓库写入运行产物。

## 已知限制

- 输入是有界批处理文件，不处理实时数据流、跨文件合并或增量快照。
- 不做用户身份合并、事件属性嵌套 JSON 路径、任意布尔表达式或 SQL。
- 分群是单字段独立分组，不计算多维交叉组合。
- 对比只基于两次已完成的本地运行，不自动重跑或修复差异。
