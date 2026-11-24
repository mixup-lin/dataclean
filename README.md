# DataClean · 自然语言驱动的本地 ETL 数据管道

> 一个本地单机、运行时零外部依赖的数据处理工具：用一句话描述需求，自动生成规则、执行清洗与分析，并产出可直接汇报的 HTML 报告与多格式结果。
>
> 核心管道仅依赖 **Python 标准库 + pandas + openpyxl**；LLM、文档解析、Web 框架均为可选依赖，缺失时自动降级，不影响核心 ETL。Web 界面采用 Vue 3 + Naive UI，但已构建为本地 bundle、运行时完全离线（无 CDN、无构建步骤）。

---

## 目录

- [核心特性](#核心特性)
- [快速开始](#快速开始)
- [三种使用方式](#三种使用方式)
- [离线能力与边界](#离线能力与边界)
- [整体架构](#整体架构)
- [关键概念](#关键概念)
- [输入与输出](#输入与输出)
- [文档结构化抽取](#文档结构化抽取)
- [测试与验证](#测试与验证)
- [安全与设计边界](#安全与设计边界)
- [文档索引](#文档索引)

---

## 核心特性

| 能力 | 说明 |
| --- | --- |
| **自然语言 → 规则 → 结果** | 描述需求后由真实 LLM 解析为结构化意图 Schema，确定性编译为规则 JSON，再执行；无 Key 时可用显式规则离线运行 |
| **多数据源** | CSV / JSON（含 JSONL）/ Excel（指定 sheet）/ SQLite（表名或只读查询，Web 可指定），支持多表 join / union 与键对齐、膨胀保护 |
| **54 个声明式算子** | 清洗、筛选、派生、聚合、占比、透视、排名、日期分桶、异常检测等，全部白名单校验、无 `eval` |
| **任务画像 + 场景化展示** | 12 类数据处理场景映射到卡片/表格/图表/摘要，按数据类型、字段特征、结果规模、展示目标自动选模板 |
| **自包含 HTML 报告** | 纯内联 SVG + 样式，无 JS、可离线打开与打印；含质量报告与任务报告两套视角 |
| **本地 Web 工作台** | Vue 3 + Naive UI 组件化界面（工作台 / 结果查看 / 文档抽取 / 后台任务），SSR 骨架 + 渐进接管，构建产物本地化、运行时零 CDN，保留键盘可达性，仅绑定 `127.0.0.1` |
| **同步 / 异步双模式** | 文档抽取可在页面选择同步等待或后台异步；异步任务有进度页与 `meta refresh` 轮询，任务列表可随时找回，无需前端框架 |
| **离线可用** | 规则执行侧（清洗、54 算子、报告、导出、Web、规则库）完全离线；仅自然语言生成规则与文档结构化抽取需 LLM |
| **可生长算子体系** | 算子在注册表单点声明，校验、编译、执行、提示词、任务画像共用同一份定义，扩展无需改核心分发 |

---

## 快速开始

### 1. 环境要求

- Python 3.9+
- 核心依赖：`pandas`、`numpy`、`openpyxl`
- 运行 Web / 查看界面：**无需 Node**；只有修改前端组件源码、需要重新打包 Vue 时才用 Node 18+（产物已随仓库提供）

### 2. 安装

```bash
# 完整安装（含全部可选依赖）
pip install -r requirements.txt

# 仅核心管道
pip install pandas numpy openpyxl
```

### 3. 配置 LLM（可选）

自然语言规则生成与文档抽取需要 LLM。复制示例配置并填写：

```bash
cp .env.example .env
```

编辑 `.env`（最小配置）：

```ini
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=你的密钥
LLM_MODEL=gpt-4o-mini
```

可选配置项：图片 / 扫描件抽取用 `LLM_VISION_MODEL`（留空则复用 `LLM_MODEL`）；`LLM_TIMEOUT`（默认 60 秒）、`LLM_MAX_RETRIES`（默认 2，仅网络错误 / 5xx 重试）。兼容 `OPENAI_API_KEY` / `OPENAI_BASE_URL` 别名。配置优先级为「显式参数 → 系统环境变量 → `.env`」，`.env` 修改后无需重启进程。检查是否生效（输出不含密钥值）：

```bash
python etl_pipeline.py llm-check
```

> 无 `LLM_API_KEY` 时，自然语言生成与文档抽取会明确报错，不再降级到关键词模板；核心 ETL 管道与显式规则执行完全不受影响。

### 4. 第一个例子

```bash
# 经典方式：按区县分组统计
python etl_pipeline.py lots.csv -f csv --group-by 区县 --fill median

# 自然语言方式：一句话完成同样的分析
python etl_pipeline.py nl "按区县分组统计每组的成交条数" --source lots.csv
```

执行后，结果落到 `output/run_<时间戳>_<随机码>/`，并在控制台打印 `run_id`、指标与产物清单。

---

## 三种使用方式

### 方式一：经典 CLI（参数化 ETL）

无需 LLM，直接对文件做清洗、聚合与报告：

```bash
# Excel：均值填充 + 月度重采样
python etl_pipeline.py sales.xlsx -f excel --fill mean --time-col date --resample M

# SQLite：自定义查询 + Z-Score 异常值裁剪
python etl_pipeline.py app.db -f sqlite --query "SELECT * FROM users" \
  --outlier zscore --outlier-action clip

# JSON：生成透视表
python etl_pipeline.py events.json -f json --pivot-index region \
  --pivot-columns channel --pivot-values amount
```

完整参数见 `python etl_pipeline.py -h` 或[技术文档](Python数据处理自动化脚本.md#五命令行参数说明)。

### 方式二：自然语言 CLI（`nl` 子命令）

```bash
# 仅生成规则，不执行
python etl_pipeline.py nl "中位数填充缺失，剔除常数列，导出 excel" \
  --source lots.csv --rules-only

# 生成并执行
python etl_pipeline.py nl "筛选成交价大于 0 并按成交价降序" --source lots.csv

# 离线重跑已保存的规则（无需 Key）
python etl_pipeline.py nl "重跑" --rule-file output/run_xxx/rule.json

# 从规则库按名称/ID 载入，并把本次规则保存到规则库
python etl_pipeline.py nl "分析成交" --save-rule 成交分析
python etl_pipeline.py nl "重跑" --rule 成交分析
```

`nl` 默认使用 `schema` 生成主路径；`--generation-path legacy` 仅供回归诊断。

### 方式三：本地 Web 工作台

```bash
python etl_pipeline.py web --port 8765
```

浏览器打开 `http://127.0.0.1:8765`。导航栏有四个入口：

| 页面 | 路径 | 内容 |
| --- | --- | --- |
| 自然语言工作台 | `/` | 需求输入、任务快捷标签、主表/附加表上传（含 SQLite 表名与只读查询）、Schema 解析面板、规则库（保存/加载/删除/直接执行）、规则 JSON 编辑、执行记录 |
| 结果查看 | `/workbench-runs` → `/result/<run_id>` | 自动跳转到最新结果；KPI 卡、SVG 图表、结果表分页与附表切换、下载区、阶段耗时 |
| 文档抽取 | `/extract` | 文档路径、字段 Schema、fields/tables 双视图、置信度与原文证据；可选择同步或异步 |
| 后台任务 | `/jobs`、`/job/<id>` | 异步任务的进度页与任务列表；有活动任务时低频自动刷新，结果完成后回到常规结果页 |

此外 `/plan`、`/run`、`/execute`、`/rules/*`、`/download/<run_id>/<file>` 为表单提交与下载路由；`/job/<id>/status` 返回任务状态 JSON。所有下载与结果路由都经过文件白名单与路径校验。

**界面实现**：服务端先输出完整的 SSR 骨架（无 JS 时仍可读、可提交，也是所有回归断言的依据）；浏览器加载本地 `static/vendor/naive-vue.js`（Vue 3 + Naive UI 的 esbuild IIFE 产物）与 `app.js` 后，Vue 挂载并把视觉层接管为 `n-input / n-select / n-button / n-card / n-data-table / n-tag / n-alert / n-message` 等组件。提交复用原生 `<form>` POST，字段名与 action 与 SSR 完全一致，后端契约零改动。

构建脚本与验证工具在 `webui/`（仅构建期需要 Node，运行时不需要）：

```bash
cd webui
npm ci                 # 安装构建依赖（首次需联网）
npm run build          # 重建 static/vendor/naive-vue.js（约 896 KiB，含 Vue 编译器）
npm run verify         # jsdom 验证：4 页挂载 + 6 项提交契约
```

产物已随仓库提供，`npm` 与 Node 不是使用 DataClean 的前置条件；只有修改前端组件源码时才需重新构建。

---

## 离线能力与边界

按"是否需要联网 / LLM Key"划分如下。**规则的"执行侧"完全离线，只有"生成侧"需要 LLM**：

| 离线可用（无需网络与 Key） | 需要网络 + LLM Key |
| --- | --- |
| 经典 CLI 管道：读多源 → 清洗 → 54 算子 → 聚合 → 报告 → 导出 | `nl "描述需求"`：自然语言生成规则（真实 LLM 填 Schema） |
| `nl "描述" --rule-file r.json` 或 `--rule 名称`：跳过 LLM 直接执行已有规则 | `extract 文档 ...`：文档结构化抽取（节能模式只减少送入片段，仍需调用） |
| `--rules-only`：只输出规则 JSON | 真实模型评测脚本：`measure_prompt_accuracy.py` / `evaluate_schema_accuracy.py` |
| 多格式导出（CSV / XLSX / JSON）、场景展示样例 `showcase` | |
| HTML 报告、规则库管理、Web 工作台全部页面、`cleanup`、`llm-check`、全量测试 | |

也就是说：**只要有规则 JSON（手写的、保存过的，或在 Web 上编辑的），从清洗到报告到导出全程不需要联网**。无 `LLM_API_KEY` 时，自然语言生成与文档抽取会明确报错，不会降级到关键词模板或 Mock；核心 ETL 不受影响。

---

## 整体架构

```mermaid
flowchart LR
    A[自然语言需求 + 数据源] --> B[LLM 填结构化意图 Schema]
    B --> C[Schema 校验 + 确定性编译]
    C --> D[规则 JSON v2.0]
    D --> E[白名单校验 / 语义复评]
    E --> F[read → clean → transform → steps]
    F --> G[任务画像 + 场景化展示模板]
    G --> H[HTML 报告 + 多格式导出]
```

**分层与单一事实源**：`ops/registry.py` 的 `OpSpec` 是算子的唯一声明处，契约校验、编译降解、提示词目录、执行分发、任务画像全部从注册表读取。核心链路 `webapp.py` 与 CLI 子命令共用 `rules.py` / `rule_executor.py` / `doc_extract.py` / `exporters.py`，不反向依赖 Web 层。

**主要模块**：

| 模块 | 职责 |
| --- | --- |
| `etl_pipeline.py` | 核心管道、CLI 前缀分派、质量报告 |
| `llm_provider.py` | LLM Provider 协议、OpenAI 兼容实现、`.env` 加载、JSON 容错解析 |
| `rules.py` | Schema 解析主路径、规则契约校验、Prompt 组装、生成后闭环修复 |
| `ops/` | 算子注册表、意图 Schema、确定性编译器、语义评测器、内置算子 |
| `rule_executor.py` | 规则执行、阶段耗时、任务画像、报告与导出编排 |
| `task_profile.py` / `display_templates.py` | 任务类型识别、场景化展示计划与块渲染 |
| `charts_svg.py` / `report_task.py` | 纯 SVG 图表、自包含单页任务报告 |
| `doc_extract.py` | 文档解析与结构化抽取 |
| `webapp.py` / `pages/` / `static/style.css` | 本地 Web 工作台、SSR 模板与无障碍样式 |
| `static/vendor/` / `webui/` | 本地化 Vue 3 + Naive UI bundle、前端组件入口与构建/验证脚本 |

完整模块清单见[技术文档](Python数据处理自动化脚本.md#四源码结构与模块清单)。

---

## 关键概念

### 规则契约 v2.0

规则是生成与执行之间**唯一的中间产物**，采用受限白名单 DSL（非自由表达式、无代码生成）：

```json
{
  "version": "2.0",
  "intent": "按小区分组统计成交条数",
  "source": {"path": "lots.csv", "format": "csv"},
  "options": {"fill_missing": "median", "outlier_method": "iqr"},
  "steps": [
    {"op": "filter", "column": "成交价(元)", "operator": "gt", "value": 0},
    {"op": "aggregate", "by": ["小区"], "column": "小区", "func": "count", "alias": "条数"}
  ],
  "output": {"formats": ["csv", "xlsx", "json"], "report": true}
}
```

`source.path` 与 `intent` 以调用方输入为准，LLM 输出始终视为不可信文本，经白名单规整后才执行。列名不一致时通过 `column_aliases` 显式映射，程序不内置语义词典、不做相近列猜测。

### 结构化意图 Schema v1.0（主路径）

默认生成链路为：描述 → 真实 LLM 填充受约束的意图表 → 结构校验 → 确定性编译 → 结构复评。Schema 用 `kind` 描述算子（如 `dedupe` / `join` / `filter` / `date_part` / `aggregate` / `rate` / `sort`），校验输出 `unknown_column` / `unresolved_columns` / `unknown_source` 等结构化错误码。多表关联的 `right` 必须是已声明的附加表别名，维表列与派生列共同纳入真实列集合。旧「模型直接输出规则 + 中文正则编译」链路保留为 `legacy`，仅供诊断。

### 场景化展示模板

`display_templates.py` 把数据处理场景与展示方式解耦，同一结果可切换模板：

| 场景 | 主展示 |
| --- | --- |
| 数据清洗 / 统计分析 / 分组汇总 | 卡片 + 条形/分布图 + 明细表 + 口径说明 |
| 趋势对比 / 排名 / 分布 | 折线/横向条/柱状 + 峰值卡片 + 逐行表 |
| 异常识别 / 占比构成 / 相关 / 透视 / 多表合并 | 环形/散点/热力图 + 比率卡 + 附录 |
| 明细查看 | 卡片 + 表格（不渲染图表） |

四维选择依据：数据类型、字段特征、结果规模、展示目标（`demo` / `report` / `guide`）。新增展示形态只需注册块渲染器，新增场景只需增加一条 `ScenarioSpec`。

---

## 输入与输出

支持 CSV / JSON（JSONL）/ Excel / SQLite，以及多数据源关联。每次执行落到 `output/run_<时间戳>_<随机码>/`：

| 文件 | 说明 |
| --- | --- |
| `result.csv` / `result.xlsx` / `result.json` | 处理结果，Excel 含附表 sheet |
| `table_1.csv … table_6.csv` | 附表：维度汇总、关联统计 |
| `rule.json` | 本次规则，可编辑后重跑 |
| `execution_summary.json` | 阶段耗时、指标、步骤日志、任务画像、产物清单 |
| `task_report_*.html` | 任务报告：KPI、图表、结论、结果表、可折叠质量附录 |
| `quality_report_*.html` | 静态质量报告 |

生成可直接用于演示/汇报/使用说明的场景样例：

```bash
python etl_pipeline.py showcase --output-dir output/showcase
```

产出场景总览页 `index.html` + 11 类场景 × 3 种目标的 33 个自包含 HTML。

---

## 文档结构化抽取

把 PDF / DOCX / TXT / 图片等文档解析为结构化字段与表格：

```bash
python etl_pipeline.py extract samples/contract.txt \
  --schema "*合同编号:string;主体名称:string;金额:number"
```

流程：扩展名解析 → `ParsedDocument` 中间结构 → 文本走文本模型、图片走视觉模型 → 按 Schema 归一化 → `extraction.json` / `extraction.csv`。输出含 `fields` / `tables` / `confidence` / `evidence`，DOCX 内嵌表格直接结构化。输出的 JSON / CSV 可送入清洗管道继续处理。

- **批量**：`extract` 可一次传多个文档，用 `--workers 1-8` 控制并发，输出批量汇总。
- **节能模式**：`--eco`（Web 抽取页上对应「启用节能模式」勾选框）先按字段关键词定位相关片段，只把命中片段送给 LLM，用 `--eco-window` / `--eco-max-snippets` / `--eco-max-chars` 控制送量；定位失败时自动回退全文。
- **同步 / 异步**：Web 抽取页可选择异步提交，任务转入 `/jobs` 后台执行，进度页自动轮询，结果完成后回到常规结果页；命令行始终是同步执行。

> 文档抽取需要 LLM：本功能依赖文本 / 视觉模型，无 Key 时不可用。文档解析（扩展名识别、文本/DOCX 表格提取）在本地完成。

---

## 测试与验证

```bash
# 全量离线测试（默认不调用在线模型）
pytest -q

# 仅核心与配置回归（无需 Key / 网络 / 可选依赖）
pytest -q tests/test_offline_flow.py tests/test_llm_config.py

# 生成质量与前端链路门禁
pytest -q tests/test_generation_gate.py tests/test_generate_execute_chain.py

# 前端可访问性与 Naive UI 层契约
pytest -q tests/test_ui_accessibility.py tests/test_naive_ui_layer.py

# Vue 真实挂载 / 提交契约（需构建期依赖 jsdom）
cd webui && npm run verify
```

默认 `pytest` 不调用在线模型（带 `real_llm` 标记的用例默认跳过）。需显式开启时：

```bash
pytest -q -m real_llm                    # 运行真实模型用例（需 Key，偶发 429 限流，重试即可）

python measure_prompt_accuracy.py        # 提示词正确率 → output/prompt_quality_report.json
python evaluate_schema_accuracy.py       # Schema 主路径正确率 → output/schema_accuracy_report.md
python verify_sqlite_matrix.py           # SQLite 全场景 /plan → /execute → /result → output/sqlite_matrix_report.md
```

**最近基线**：离线 **872 passed / 29 skipped**；SQLite 全场景 **26/26**（数据源形态、Schema 分析、多表关联、13 个既有场景迁移、空表与异常 SQL）；Vue 层 jsdom 验证 4/4 页挂载、6/6 提交契约；提示词场景 33/33、Schema 主路径 25/25 正确（模型 `glm-4-flash-250414`）。

---

## 安全与设计边界

- **不可信输入**：LLM 输出仅作文本解析，绝不直接执行；规则采用受限白名单 DSL，防注入、可校验、可审计。
- **路径安全**：`source` 与下载路由校验本地普通文件，拒绝 URL、空字节与目录穿越；上传文件名清洗并限制在 `uploads/` 内。
- **SQLite 只读**：表名按标识符转义，读取连接统一开启 `PRAGMA query_only = ON`；自定义 SQL 只能读，不能写入或删除数据库。
- **数据保护**：过滤基于清洗前的原始列值，避免填充把「未成交」误判为已成交；缺失密集时自动回退，防止删空数据。
- **可观测性**：日志只记录模型、耗时、响应长度与失败原因，绝不输出 API Key、文档全文或完整响应；CSV 用 `utf-8-sig` 便于 Excel 直接打开，数值不使用科学计数法。
- **产品定位**：本地单机工具，不提供鉴权、多用户与网络暴露能力；Web 仅绑定 `127.0.0.1`。联网暴露与多用户场景属于后续规划。

---

*本地单机工具 · 核心管道仅依赖 pandas / numpy / openpyxl · 无 Key 时自然语言生成与文档抽取不可用，显式规则可离线执行。*
