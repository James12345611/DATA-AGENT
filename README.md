# DATA-AGENT · Text2SQL V1

> **仓库定位**（原仓库说明保留）：本仓库用于 DATA AGENT 设计与演示，服务生产中的智能取数、报表生成与数据分析以及数据工程等方面业务。
>
> **当前项目**：Text2SQL V1 —— 基于 [`text2sql_V1.md`](text2sql_V1.md) 的 NL2SQL 最小可用闭环
> （LangGraph 编排 + 业务 Catalog + sqlglot 确定性安全校验 + 只读执行与有限重试），
> 面向电商 6 张业务表的自然语言取数。

---

基于 `text2sql_V1.md` 开发文档实现的 **Text2SQL 最小可用闭环（MVP）**：
用 LangGraph 编排「意图抽取 → Schema Linking → SQL 生成 → 安全校验 → 只读执行 → 有限重试」，
只对 6 张业务视图开放查询面，任何时刻都不让模型自由发挥表名、字段名与 SQL 安全性。

> 不知道怎么测、怎么看日志？直接看 **[docs/测试指南.md](docs/测试指南.md)**：
> 四条上手命令、pytest 输出怎么读、逐节点 `trace` 观测、错误类型对照表、一页纸排查流程。

```text
自然语言问题
  -> information_extraction   结构化意图（维度/指标/过滤/时间）
  -> schema_linking           Catalog 确定性检索 + 覆盖度校验 + 表白名单
  -> generate_sql             单条 MySQL 只读 SQL
  -> validate_sql             sqlglot AST 确定性校验（12 项）
  -> execute_sql              只读账号 + 只读事务 + 30s 超时
  -> regenerate_sql           可恢复错误有限次修正（最多 2 次）
  -> 结构化结果输出（result_rows / result_columns / row_count / status / error）
```

---

## 1. 交付物一览

| 类别 | 位置 | 说明 |
| --- | --- | --- |
| 核心库 | `src/text2sql/` | 图、节点、Catalog、校验器、只读执行器、配置、状态契约 |
| Catalog 元数据 | `catalog/catalog_v1.yaml` | 6 张业务表的粒度、字段、同义词、指标口径、JOIN 规则 |
| CLI | `src/text2sql/cli.py` | `check` / `ask` / `catalog` / `validate` 四个子命令 |
| 单元与契约测试 | `tests/` | 191 个用例（含 13 个真实模型用例，默认跳过） |
| 验收用例 | `tests/test_acceptance.py`、`src/text2sql/acceptance.py` | 文档 8.1/8.2/8.3 全部用例 |
| 验收报告 | `reports/` | 离线/真实模型 × 样例/全量 四份报告（含 SQL 与结果） |
| 运维脚本 | `scripts/` | 只读账号、全量导入、业务对象物化、验收报告、环境兼容 shim |

## 2. 快速开始

### 2.1 环境

```powershell
# 已在本机创建：Python 3.11 虚拟环境 + 依赖
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

依赖：`langgraph`、`langchain-core`、`langchain-openai`、`sqlglot`、`SQLAlchemy`、`PyMySQL`、
`cryptography`、`python-dotenv`、`PyYAML`、`pytest`。

### 2.2 配置

复制 `.env.example` 为 `.env`（已生成，含真实凭据，`.gitignore` 已忽略）：

```dotenv
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=ecommerce_text2sql
DB_USER=text2sql_readonly       # 只读账号，只能 SELECT 6 张业务视图
DB_PASSWORD=********
SQL_DIALECT=mysql
SQL_MAX_ROWS=100
SQL_TIMEOUT_SECONDS=30
SQL_MAX_RETRIES=2
CATALOG_VERSION=v1
SQL_LIMIT_POLICY=normalize      # normalize=补/截断 LIMIT；reject=直接拒绝
CATALOG_SOURCE=yaml             # yaml=静态 Catalog；mysql=叠加 catalog_* 元数据
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=********
```

### 2.3 只读账号与数据

```powershell
$env:ADMIN_DB_USER="root"; $env:ADMIN_DB_PASSWORD="<管理员密码>"
.\.venv\Scripts\python.exe scripts\setup_readonly_user.py
```

脚本会创建 `text2sql_readonly`，只授予 6 张业务视图的 `SELECT`，并**验证**：建表被拒（1142）、
`raw_orders` 不可读（1142）。业务视图由父项目 `mysql/` 包创建：

```powershell
# 样例数据（1000 用户 / 3000 行为 / 1000 订单 / 1000 曝光）
& ..\mysql\scripts\load_csv.ps1 -Mode sample -User root
# 全量数据（50 万用户 / 300 万行为 / 52 万订单 / 100 万曝光）
$env:ADMIN_DB_PASSWORD="<管理员密码>"
.\.venv\Scripts\python.exe scripts\load_full_data.py --mode full
```

> 两种数据规模共用同一套 schema、Catalog 与节点契约（文档 8.4.9）：切换数据后
> `python -m text2sql check` 与验收用例会重新校验视图字段与 Catalog 是否一致。

#### 全量数据下的物化（实测结论）

`mart_funnel_summary`、`mart_retention_cohort` 等对象是**视图**，每次查询都要对
300 万行行为明细做 `COUNT(DISTINCT ...)`。实测：样例数据毫秒级，全量数据下单次查询
需要 30-300 秒，会超过 `SQL_TIMEOUT_SECONDS=30` 并触发超时保护（这是预期行为，不是缺陷）。

两种应对方式（推荐第一种，名称/字段/口径完全不变）：

```powershell
# 1) 物化：把 6 张业务对象改写成同名物理表（带粒度主键与常用索引）
$env:ADMIN_DB_PASSWORD="<管理员密码>"
.\.venv\Scripts\python.exe scripts\materialize_marts.py --mode materialize
.\.venv\Scripts\python.exe scripts\materialize_marts.py --mode report      # 只读账号计时

# 2) 或提高超时（保留实时视图口径）
#    在 .env 中设置 SQL_TIMEOUT_SECONDS=120
```

物化后 Catalog、白名单、只读授权、节点契约都不需要改动；重新导入原始数据后需要再次执行
`materialize`。`--mode restore` 可以把对象还原成父项目 `mysql/sql/02_views.sql` 定义的视图。

实测（50 万用户 / 300 万行为 / 52 万订单 / 100 万曝光，物化后）：

| 对象 | 行数 | 查询耗时 |
| --- | ---: | ---: |
| `dim_user` | 500,000 | < 0.1s |
| `mart_user_behavior_summary` | 437,739 | ~0.1s |
| `mart_user_order_summary` | 500,000 | ~0.1s |
| `mart_campaign_summary` | 20 | < 0.1s |
| `mart_funnel_summary` | 6 | < 0.1s |
| `mart_retention_cohort` | 120 | < 0.1s |

### 2.4 使用

```powershell
# 一致性与连通性自检（配置、Catalog、视图字段、JOIN 规则）
.\.venv\Scripts\python.exe -m text2sql check

# 提问（真实模型）
.\.venv\Scripts\python.exe -m text2sql ask "各活动的曝光量、点击率和转化率" --show-sql
.\.venv\Scripts\python.exe -m text2sql ask "按渠道统计用户数" --json

# 逐节点观测：每个节点的输入输出、选表理由、耗时（排错首选）
.\.venv\Scripts\python.exe -m text2sql trace "行为次数最多的 10 个用户"

# 详细日志 + 落盘（事后排查用）
.\.venv\Scripts\python.exe -m text2sql -v --log-file logs\run.log ask "按渠道统计用户数"

# 离线确定性客户端（无网络、无 Key 也能跑通全链路）
.\.venv\Scripts\python.exe -m text2sql ask "各行为阶段的去重用户数" --offline --show-sql

# 只观察 Catalog 召回 / 只跑安全校验
.\.venv\Scripts\python.exe -m text2sql catalog "点击率最高的 5 个活动"
.\.venv\Scripts\python.exe -m text2sql validate --sql "SELECT channel FROM dim_user LIMIT 500"
```

也可以使用便捷脚本（自动设置 venv、临时目录与编码）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 check
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 test
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 acceptance
```

### 2.5 测试与验收

```powershell
.\.venv\Scripts\python.exe -m pytest -q                 # 201 passed, 13 skipped（真实模型用例）
.\.venv\Scripts\python.exe scripts\run_acceptance.py            # 离线报告
.\.venv\Scripts\python.exe scripts\run_acceptance.py --live     # 真实模型报告
$env:RUN_LIVE_LLM=1; .\.venv\Scripts\python.exe -m pytest tests/test_e2e_live_llm.py -v
```

测试怎么读、日志怎么看、报错怎么办：见 **[docs/测试指南.md](docs/测试指南.md)**。

## 3. 架构与实现约定

### 3.1 图与节点

| 节点 | 读取状态 | 写入状态 | 失败去向 |
| --- | --- | --- | --- |
| `information_extraction` | `question`、`messages` | `rewrite_question`、`info_entities`、`status`、`error` | `failed` |
| `schema_linking` | `rewrite_question`、`info_entities` | `candidate_tables`、`selected_tables`、`status`、`error` | `failed` |
| `generate_sql` | `rewrite_question`、`info_entities`、`selected_tables`、`previous_sql_errors` | `sql`、`retry_count`、`status`、`error` | `validate_sql` / `failed` |
| `validate_sql` | `sql`、`selected_tables` | `status`、`error`（+ LIMIT 规范化时的 `sql`） | `failed` |
| `execute_sql` | `sql`、`retry_count` | `result_rows`、`result_columns`、`row_count`、`status`、`error`、`previous_sql_errors` | `regenerate_sql` / `failed` |
| `regenerate_sql` | `sql`、`previous_sql_errors`、`retry_count`、`selected_tables` | `sql`、`retry_count`、`status`、`error` | `validate_sql` / `failed` |

三个条件边函数（`graph.py`）与文档 5.10 完全一致：`execute_sql_conditional_edges` 只有在
`recovery_strategy == "retry"` 且 `retry_count < SQL_MAX_RETRIES` 时才进入 `regenerate_sql`；
`validate_sql` 的安全拒绝在 V1 **直接** `failed`，不允许模型反复重试危险 SQL。

### 3.2 Catalog（业务语义注册中心）

* 唯一权威来源是 `catalog/catalog_v1.yaml`：表级（`grain`/`description`/`synonyms`/`primary_key`/
  `default_time_columns`）、字段级（`role`/`aggregation`/`business_name`/`synonyms`/`unit`）、
  指标口径（`formula` + 比率指标的分子/分母字段）、JOIN 规则（允许与**显式禁止**）。
* 检索是确定性的（无向量、无 LLM）：归一化 → 命中指标 → 命中维度 → 命中表语义 → 问题文本兜底；
  权重可解释，`score >= 0.30` 才进入候选，最多 4 张表，未命中时返回空列表而**不猜表**。
* `schema_linking` 负责覆盖度：每个抽取出的指标/维度/过滤都必须解析到具体字段，否则
  `schema_linking_error`（例如“商品名称/品牌”这类 V1 不存在的字段）。
* 启动一致性校验（文档 6.7）：静态校验（白名单、grain、主键、指标口径、同义词冲突、JOIN 字段类型）
  + 实时校验（`information_schema` 中视图字段必须与 Catalog 完全一致），失败即拒绝启动。

### 3.3 SQL 安全校验（`validate_sql`）

固定 `sqlglot` MySQL AST + 确定性规则，正则仅作补充：

1. 单语句；2. 顶层 `SELECT` / `WITH ... SELECT`；3. 禁止 `INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE/GRANT/SET/EXPLAIN...`；
4. 表必须在 6 张白名单内且属于 `selected_tables`；5. 字段必须存在且属于已选字段；
6. 禁止 `information_schema` / `mysql` / `performance_schema` / `sys` / `catalog_*`；
7. JOIN 风险：只允许登记在册的 `user_id` 1:1 关联，禁止自连接与重复引用，禁止自包含汇总表与用户表关联；
8. `LIMIT` 必须存在；9. 超过 100 行时按 `SQL_LIMIT_POLICY` 截断或拒绝；10. 注释 / 多语句 / 未闭合字符串；
11. 只允许 MySQL 8.0 函数（`DATE_TRUNC`、`APPROX_COUNT_DISTINCT`、`TO_CHAR`、`NVL` 等直接拒绝）；
12. 禁止 `SELECT *`（`COUNT(*)` 允许）。

### 3.4 只读执行（`execute_sql`）

* 专用只读账号 + `SET SESSION TRANSACTION READ ONLY` + `SET SESSION max_execution_time=30000`；
* 连接池 `pool_pre_ping` / `pool_recycle`；连接失败最多重试 2 次（只针对建连，不针对 SQL）；
* PyMySQL 未开启多语句标志，双层阻断多语句 SQL；
* 结果统一转换（`Decimal→float`、`datetime→isoformat`），空结果集是**成功**而不是失败；
* 错误分类（errno → `error_type` / `recovery_strategy`）：`1054/1064/1146→retry`、
  `3024/1205/1317→timeout`、`1045/1142/1227→permission_error(surface)`、
  `2003/2006/2013→connection_error(surface)`、`1038/1114/1206→resource_error(abort)`；
* 任何错误消息、状态、日志都经过 `settings.redact()`，数据库密码永不外泄。

### 3.5 重试语义（文档 8.3 的落地口径）

| 场景 | 行为 |
| --- | --- |
| 可恢复执行错误（字段不存在/语法/表不存在） | 写入 `previous_sql_errors` → `regenerate_sql`，最多 `SQL_MAX_RETRIES=2` 次 |
| 重试预算耗尽 | `retry_exhausted` + `abort`，`status="failed"` |
| 查询超时 | 允许重试 **1** 次，仍超时则 `timeout` + `surface_to_user` |
| 连接/权限错误 | 不重试，`surface_to_user` |
| 安全校验拒绝 | 不进入 `regenerate_sql`，直接 `abort` |

## 4. 与开发文档的对应与偏差说明

完全对齐的部分：数据模型与 6 张表边界、State 契约字段命名、节点职责与读写字段、条件边函数、
Catalog 资源模型（表/字段/指标/JOIN/检索接口/一致性校验）、运行配置项、验收用例集合。

实现中显式记录的口径选择（均为文档允许范围内的确定性决策）：

1. **`validate_sql` 可以改写 `sql`**：文档 5.6 第 8/9 条允许“自动补充 LIMIT 100 或拒绝”“拒绝或改写为 100”。
   默认 `SQL_LIMIT_POLICY=normalize`（补/截断），可切换为 `reject`（拒绝）。改写仅限 LIMIT。
2. **可恢复错误的状态取值**：`execute_sql` 对可恢复错误写 `status="pending"` + `error`（供路由函数读取
   `recovery_strategy`），终止性错误才写 `status="failed"`；失败出口节点统一兜底 `status="failed"`。
3. **破坏性请求的确定性拦截**：`information_extraction` 在任何 LLM 调用之前用规则识别
   删除/更新/插入/清空/DDL 意图，返回 `error_type="unsafe_request"`、`recovery_strategy="abort"`
   （文档 8.2 N01 要求“直接拒绝”）。
4. **`dim_user` 作为用户粒度 JOIN 锚点**：当问题需要用户粒度汇总表时，`dim_user` 一并入选
   （文档 2.3 的标准写法），并以“锚点候选”的形式写入 `candidate_tables`，保证
   `selected_tables ⊆ candidate_tables`（文档 5.4）。
5. **比率指标口径**：Catalog 的指标语义额外登记 `numerator_column`/`denominator_column`，
   “点击后转化率”= `converted_count / clicked_count`，跨行汇总时用分子分母重算，
   不把 `conversion_rate`（曝光转化率）当作点击后转化率（文档 8.2 N04、6.3.3）。
6. **`user_id` 关联键校验加强**：不仅要求 ON 条件里出现 `user_id`，还要求它出现在等值两侧。
7. **离线确定性客户端**：`src/text2sql/offline_llm.py` 在没有 `LLM_API_KEY` 或显式 `--offline` 时启用，
   用规则完成意图抽取与 SQL 生成，保证测试与 CI 完全可复现；真实模型走 `langchain-openai`
   （DeepSeek/任意 OpenAI 兼容端点）。
8. **Catalog 存储层**：`catalog_*` 表字段少于文档契约（缺 `role`/`aggregation`/`allowed`/JOIN 规则），
   因此 YAML 为权威，`CATALOG_SOURCE=mysql` 时用存储层**补充**业务名与同义词，且永不扩大暴露面。
9. **AST 作用域简化**：字段解析使用语句级别名映射（不做完整作用域树），未识别字段仍会被拒绝；
   CTE 输出列按投影名解析。
10. **`Explain` 节点兼容**：sqlglot 30 将 `EXPLAIN` 解析为 `Describe`，校验器对节点名做了版本兼容处理，
    `EXPLAIN` 默认一律拒绝。

## 5. 目录结构

```text
DATA-AGENT/                          # 本仓库根目录即 Text2SQL V1 项目
├─ catalog/catalog_v1.yaml          # 6 张业务表的 Catalog（权威元数据）
├─ src/text2sql/
│  ├─ config.py state.py errors.py guardrails.py
│  ├─ catalog/{models,service,mysql_source}.py
│  ├─ db/executor.py                # 只读执行器
│  ├─ nodes/{information_extraction,schema_linking,generate_sql,validate_sql,execute_sql,regenerate_sql}.py
│  ├─ validation.py                 # sqlglot 确定性校验器
│  ├─ llm.py offline_llm.py prompts.py
│  ├─ graph.py cli.py acceptance.py
├─ tests/                           # 201 个用例
├─ scripts/
│  ├─ setup_readonly_user.{py,sql}  # 只读账号
│  ├─ load_full_data.py             # 全量/样例数据导入
│  ├─ materialize_marts.py          # 业务对象物化 / 还原
│  ├─ run_acceptance.py             # 验收报告
│  ├─ copy_project.py               # 导出项目到其它目录（自动排除 .env/缓存）
│  ├─ dev.ps1 run_in_sandbox.py sandbox_site/  # 环境兼容
│  └─ debug_extraction.py debug_ast.py debug_live_state.py
├─ docs/                            # 测试指南、交付说明
└─ reports/                         # 验收报告（离线/真实 × 样例/全量）
```

## 6. 仓库与更新代码

本项目已托管在 **https://github.com/James12345611/DATA-AGENT**（仓库根目录即本项目）。

```powershell
# 首次：本机已配置好 origin 与 SSH
git remote -v          # git@github.com:James12345611/DATA-AGENT.git

# 日常：改完代码跑测试，然后提交推送
.\.venv\Scripts\python.exe -m pytest -q
git add -A
git commit -m "fix: 你的改动说明"
git push origin main
```

注意事项：

- **`.env` 已被 `.gitignore` 忽略，永远不会被提交**；仓库里只有 `.env.example` 模板。
  提交前可自查：`git status --short` 里不应出现 `.env`，`git grep --cached -n "DB_PASSWORD=" ` 应只匹配到模板占位符。
- 若 `git push` 报 `couldn't create signal pipe`（Git 自带 ssh 与沙箱/安全软件冲突），
  改用系统 OpenSSH 即可，本仓库已在 `.git/config` 里设置好：
  `git config core.sshCommand "C:/Windows/System32/OpenSSH/ssh.exe"`。
- 只想看远程有什么：`git fetch origin && git log --oneline origin/main -5`。

## 7. 后续演进（V2 建议）

1. 按文档 6.8 第 6 步把 Catalog 迁移到 MySQL `catalog_*`（补齐契约字段后以存储层为唯一来源）；
2. 明细层（DWD）开放：增加重复聚合风险规则与 `1:N` JOIN 支持，`retry_with_new_table` 路由落地；
3. SQL 置信度与 human-in-loop：低置信度转人工确认；
4. 指标口径版本化 + 回归基准（同一问题集对比 SQL 语义与结果）；
5. 观测：节点级耗时、Catalog 命中率、重试率、拒绝原因分布。
