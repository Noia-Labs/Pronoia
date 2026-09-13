# Pronoia

> **Hunt events. Trace echoes.**
> 对话式 AI 金融事件分析工作台：提问即研究。

Pronoia 是一个开源的对话式 AI 金融研究工作台。
主理人 Agent 调用 **akshare 真实数据技能**，流式输出结论，并把 K 线、事件研究曲线、
数据表、证据与研究报告**沉淀为可回看的研究资产**。深度问题可切换「研究团队」模式，
多专家 Agent 并行作业、复核员把关。

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)]()
[![React](https://img.shields.io/badge/React-18-61dafb.svg)]()

---

## ✨ 特性

- **对话式研究工作台**：左栏研究案例（Case）、中栏对话流、右栏产出物面板。
  每个 Case 持久化（SQLite），刷新不丢，随时回看、继续追问。
- **85 个真实数据技能（Skill）**：日K行情、指数、板块、个股新闻、全局快讯、公告检索、
  财务摘要/指标、研报评级、龙虎榜、融资融券、宏观 CPI/PPI/PMI/GDP/国债收益率、
  **事件研究法（AR/CAR）**、股票搜索——全部走 akshare 免费接口，零 mock。
  另含 **6 个市场分析 Skill**（公告子类型分类 / T0 AR 主动被动分解 / 事前漂移非线性映射 /
  A 股并购分析思维 / A 股财报分析思维 / 美股并购分析思维），按 market × event_type 路由，
  替代一刀切评分卡。
- **Agent 团队模式**：Planner 拆解任务 → 事件猎手 / 行情分析师 / 基本面分析师并行执行 →
  主理人综合 → 复核员逐条核对「数据事实 vs 模型推断」→ 流式输出。
- **产出物（Artifacts）体系**：工具结果自动生成 K线图、CAR曲线、数据表、证据卡片，
  对话内 handoff，右栏大视图查看；一键生成四段式研究报告（数据事实/分析推断/风险/免责声明）。
- **过程透明**：思考过程、每次工具调用的参数与结果、每个数字的来源接口（如
  `akshare.stock_news_em`）全部可见、可追溯。
- **密钥安全**：LLM Key 只存后端 `.env`，绝不下发浏览器。
- **🧪 统一回测工作台**：回测拆分为事件模型、量化模型、运行记录、Pronoia 多模型评测
  （Model Lab）和数据管理五个独立页面。事件预测与可选问答并行执行；量化模型只评测
  信号、仓位和投资表现。详见下方独立章节。
- **🔌 多 API Pronoia 评测**：在同一事件集与题库下选择多个候选模型 API，分别查看预测
  和问答任务；问答支持独立裁判模型或人工八维评分，结果可在页面查看并导出 Markdown、
  JSON 和 CSV。模型连接可切换为 Pronoia 默认 API，真实密钥始终留在服务端环境变量中。
- **多 horizon 标签体系**：Oracle 标签覆盖 T+3 / T+7 / T+15 / T+30 / T+60 五个时间窗口的
  CAR，并加权平均为 avg_all（t3=35% / t7=28% / t15=20% / t30=12% / t60=5%），
  平滑短期波动；另含 consensus66 一致性指标（多窗口同向比例）。可指定
  `--primary-oracle-horizon avg_all` 作为主证据，避免短期方差过大。
- **Strict / Lenient 双口径 ACC**：Strict 要求预测方向与 Oracle 完全一致；
  Lenient 仅在双方均非 neutral 时计分，更公平评估方向判断能力。另输出 12 个 horizon 的
  ACC + confidence 分桶校准表（Spearman ρ）。
- **真实数据集治理**：内置 5 个 × 10 条的真实小数据集（v9_1000 官方回测池分层抽样），
  零杜撰、零未来日期、100% 可点击的东方财富公告 / Yahoo SEC 原文链接、100% 有
  真实 T+3 超额收益（CAR）Oracle Label。1000 条全量回测数据集独立存放于
  `backtesting/` 目录（v1 版本）。
- **🧩 可插拔指标系统（Metrics Registry）**：指标不再硬编码，通过 `@metric` 装饰器
  注册独立计算器；核心 18+ 指标（12 个 horizon 的 ACC / avg_all strict / lenient /
  consensus66 / coverage_rate / abstain_rate / calibration_MSE / directional_bias /
  mean_CAR / by-market & by-event-type groupwise / high_confidence_acc / prior_alignment）
  分 core / extended / breakdown 三级，列表页可动态选择展示列，支持未来新增指标零侵入。
- **🏟 Arena 双赛道横向比较**：事件预测能力赛道比较正确率、覆盖率和逐事件结果；投资表现
  赛道比较收益、回撤与风险，并支持共同区间、完整并集和手动区间。时间对齐只裁剪真实
  观察值，不插值或拉伸；页面同时显示原生/对齐指标和覆盖警告。**Arena 只收录已完成、
  可复现的正式 Run**，Demo、无效或未完成结果不会进入候选。
- **📉 成本 / 效果二维散点（帕累托边界）**：Arena 详情页新增「成本/效果」Tab，
  汇总每个模型的 tokens_in / tokens_out / 耗时 step_ms / USD 估算成本，
  与效果（主口径 acc 或 综合 composite_score）画散点图，并通过**非支配排序**
  动态计算帕累托前沿线——直观看到「效果 X% 提升是否值得 Y% 额外成本」的性价比取舍。
  支持坐标轴自由切换（X：Tokens / USD / ms，Y：ACC / 综合得分），切换时前端重算前沿。

## 🏗 架构

```
┌─────────────────────────── Frontend (React 18 + Vite + Tailwind) ───────────────────────────┐
│  Sidebar(Case列表) │ ChatPanel(消息流/工具卡/产出物卡) │ RightPanel(产出物·技能·团队)         │
│  zustand store ─── api.ts (fetch + ReadableStream 解析 SSE)                                  │
└──────────────────────────────────────────┬───────────────────────────────────────────────────┘
                                           │ POST /api/chat (SSE) · REST /api/cases|skills|agents
┌──────────────────────────────────────────▼───────────────────────────────────────────────────┐
│                      Backend (FastAPI · 单进程 · SQLite 持久化)                               │
│  routes/chat.py ──► llm.run_agent()  流式 tool-call 循环（≤8轮）                              │
│                   └► agents/team.py  plan → fan-out(asyncio.gather) → synthesize → verify     │
│  skills/registry.py  @skill 注册表（统一 ok/data/meta/artifact 协议）                          │
│  skills/market·news·fundamentals·analysis ──► akshare / sina suggest（线程池+超时+降级）      │
│  db.py  cases / messages / artifacts 三表                                                    │
└──────────────────────────────────────────┬───────────────────────────────────────────────────┘
                                           ▼
                            模型 API（兼容接口）· akshare 数据源
```

## 🚀 快速开始

```bash
cp .env.example .env        # 填入模型 API（支持火山方舟、DeepSeek 等兼容接口）
./start.sh                  # 后端 :8000 + 前端 :5173
```

## 💻 CLI

仓库现在内置了一套 `Pronoia` CLI，默认直接调用现有后端 API。

```bash
./pronoia health
./p h
./pronoia agents
./pronoia skills
./pronoia case list
./pronoia case create --title "CLI 测试"
./pronoia chat "分析贵州茅台近一个月走势"
./pronoia chat "对英伟达做深度研究" --mode team --team-members event_scout,predictor
./pronoia case report <case_id>
```

常用说明：
- `./pronoia serve`：启动后端服务
- 简写入口：`./p`
- 常用简写：`h=health`、`ag=agents`、`sk=skills`、`sg=suggestions`、`q=chat`、`c=case`
- 二级简写：`./p c ls`、`./p c new --title "测试"`、`./p c get <case_id>`、`./p c rm <case_id>`、`./p c rpt <case_id>`
- `./pronoia --json ...`：JSON 输出，方便脚本集成
- `./pronoia --base-url http://127.0.0.1:8000/api ...`：指定远端或本地 API
- `./pronoia chat --verbose`：输出 tool/agent 事件
- `./pronoia chat --show-thinking`：连 thinking 片段一起打印

CLI 覆盖的子命令：
- `serve`
- `health`
- `agents`
- `skills`
- `suggestions`
- `chat`
- `case list|create|show|delete|report`
- `cache stats|clear|toggle`
- **回测（Pronoia Backtest，P0 Web 配套 CLI）**：
  ```bash
  ./pronoia bt run     --events data/xxx.events.jsonl --out /tmp/bt_ckpt/ --labels data/xxx.labels.jsonl \
                       --runner team_full --concurrency 2 --dataset-id cn_earnings_10
  ./pronoia bt score   --ckpt-dir /tmp/bt_ckpt/  --labels data/xxx.labels.jsonl   # 计算 strict / non-neutral ACC + Wilson 95% CI
  ./pronoia bt label   --events data/xxx.events.jsonl --out data/xxx.labels.jsonl   # 用真实行情打 Oracle T+3 CAR 方向标签（akshare + yfinance）
  ./pronoia bt cst     --events data/xxx.events.jsonl --out-md /tmp/cst.md          # 对照现有 ckpt 生成结构化案例汇报表
  ./pronoia bt trajectory --ckpt-dir /tmp/bt_ckpt/ --out-md /tmp/traj.md --labels data/xxx.labels.jsonl
  ```
  > 回测 Web UI 默认走 `POST /api/bt/runs`，请用上方「🧪 Pronoia 回测 Web 平台」章节访问。

Docker（单容器，后端托管前端构建产物）：

```bash
docker build -t pronoia . && docker run -p 8000:8000 pronoia
# 打开 http://localhost:8000
```

## 🧭 两种模式

| 模式 | 适用 | 链路 |
|---|---|---|
| ⚡ 快速问答 | 单一事实/单一标的查询 | 主理人 Agent + ≤8 轮工具循环 |
| 👥 深度研究团队 | 多维度深度问题 | Planner 拆 2~4 子任务 → 3 专家并行 → 综合 → 复核修正 |

试试这些问题：
- 「分析贵州茅台近一个月的事件与股价表现」
- 「对宁德时代做深度研究」（团队模式）
- 「用事件研究法看看 600519 在 2026-06-01 前后的超额收益」
- 「央行国债收益率最近怎么走？」

## 🧩 技能清单

### 数据采集 Skill（14 个 · 跨市场通用）

`search_stock` `get_stock_daily` `get_us_stock_daily` `get_index_daily` `get_sector_spot` `get_stock_news`
`get_global_news` `get_announcements` `get_financial_abstract` `get_financial_indicator`
`get_research_reports` `get_lhb` `get_margin` `get_macro` `event_study` `get_current_date`

### 市场分析 Skill（6 个 · 按 market × event_type 路由）

- `announcement_classifier`：公告子类型分类（终止 / 首次披露 / 完成 / 合规回复 / 中介意见 / 进展 / 报告书 / 财报类）
- `ar_decomposer`：T0 AR 主动收益 vs 被动收益分解（识别 alpha 来源）
- `drift_context_analyzer`：事前漂移非线性映射 + 多 horizon 持续性 + 利好出尽系数
- `cn_ma_analyzer`：A 股并购分析思维（终止/预案/合规/报告书差异化 prior）
- `cn_earnings_analyzer`：A 股财报分析思维（业绩预告/快报/正式报告差异化 prior）
- `us_ma_analyzer`：美股并购分析思维（Rule 425 / 8-K / DEFM14A 路由）

> 注：本仓库针对网络环境做了数据源适配——东财行情类接口在部分网络不可用，
> 日K默认走新浪源、腾讯源兜底；不可用的接口已在设计中剔除，不会产生幻觉数据。

## 👥 Agent 花名册

主理人 Router · 事件猎手 Event Scout · 行情分析师 Market Analyst ·
基本面分析师 Fundamentals Analyst · 复核员 Verifier · 报告撰写员 Report Writer ·
深度研究员 Deep Researcher · 事件预测员 Predictor

## 🧪 Pronoia 统一回测与模型评测平台（Web + CLI）

平台将实验创建、历史记录、跨 API 评测和数据导入分开，同时让事件模型、量化模型与
外部模型共用冻结数据版本、执行协议和结果契约。已有回测 CLI 仍可调用后端 API；以下
五个页面是完整的 Web 工作流：

| 页面 | 路由 | 负责内容 |
|---|---|---|
| 事件模型 | `/backtest/event` | 平台模型、外部事件模型 API 或已有预测结果；预测为主测试，问答可选 |
| 量化模型 | `/backtest/quant` | 平台量化规则或外部量化模型；统一撮合，不含问答 |
| 运行记录 | `/backtest/runs` | 所有 Run 的状态、进度、指标和详情 |
| Pronoia 多模型评测 | `/backtest/model-lab` | 模型连接、默认 API、评测批次、页面结果和导出 |
| 数据管理 | `/backtest/data` | 事件库、交易数据与问答问题库的统一管理 |

未实现的 API 数据采集器不会出现在数据管理页。当前导入要求填写后端可读取的本地文件
绝对路径，事件库使用 JSONL，交易数据使用 OHLCV CSV。

问题库入口为 `/backtest/data?tab=questions`：内置从 fevertest 原表迁入的 42 道综合题、
18 道共同评测题和 6 道特色能力题，并保留原有 18 道核心精选题（综合题的子集）。
支持搜索、分类、查看题干与评分依据、另存自选题库、新建自定义问题和 JSON 导出。
“用于问答评测”只带入选题并打开配置，不会自动调用模型；未配置 API 时选题会保留。
原始 Excel 仅作为来源，运行时从项目内置 JSON 幂等入库，不依赖桌面 fevertest 文件夹。

### 事件模型与量化模型

- **统一事件创建**：事件页使用一个内嵌流程，依次选择数据、模型接口、可选问答与评测设置，
  支持保存实验或开始测试。外部模型区分已登记的 OpenAI-compatible 连接和 Event Decision
  HTTP JSON 服务，后者不能直接当作聊天接口。
- **同一实验查看两项结果**：开启问答后，事件实验详情显示独立问答状态、冻结题干、完整回答、
  Raw API / Pronoia 对照和评分。预测结束不需要等待问答；裁判失败也可保留回答并人工补分。
- **事件预测必选、问答可选**：预测和问答创建为独立任务并行执行、分别更新状态；任一
  任务完成后即可先查看其结果，另一任务失败或未完成不会掩盖已有结果。
- **问答评测**：选择候选模型 API、题目、重复次数和 Raw API / Pronoia 回答路径；评分可用
  独立裁判模型、人工八维评分或混合模式。页面显示调用量预估、任务进度、对比图、回答明细
  和八维评分表，并支持 Markdown 报告、JSON 原始结果与 CSV 明细导出。
- **事件外部模型**：通过 `Event Decision v1` 返回方向、置信度与理由；平台负责统一评测。
- **量化外部模型**：通过标准契约返回 `timestamp + target_weight`；平台统一应用成交时点、
  手续费、滑点、仓位约束和基准。量化模型不创建问答任务。
- **密钥边界**：模型连接只保存 `PRONOIA_MODEL_SECRET_*` 环境变量引用，真实 token 不写入
  浏览器或公开的 Run 数据。批次创建时冻结模型、数据、题库和评分配置，切换默认 API 不会
  改写历史结果。

### Arena 双赛道与公平性

- **事件预测能力**赛道用于同口径事件 Run，展示正确率、覆盖率和逐事件对照。
- **投资表现**赛道用于具有真实资金曲线的事件、量化和外部模型 Run，展示收益、回撤、风险、
  曲线和核心指标表。
- 投资表现支持**共同区间（intersection）**、**完整并集（union）**和**手动区间（manual）**。
  对齐只裁剪真实观察值；不插值、不前向填充，也不把短历史拉伸到完整区间。完整并集和手动
  区间中的缺失段保留为空白。
- 页面同时展示原生指标与对齐指标，并给出覆盖率、样本过短以及指数/ETF 实际存续期提示。
  日/周/月选项只对真实观察值做展示降采样，不参与指标计算。
- 显式时间对齐只将原始日期窗口和源频率外置为对齐条件；正式排名仍要求数据内容快照、
  `engine_mode`、benchmark、成交语义、成本、仓位约束和评估器版本一致。Arena 使用 Run
  冻结的预测或组合结果，不会为比较而重新运行或改写策略。

可插拔指标仍由 `backend/app/event_backtest/metrics_registry.py` 注册；无真实数据时结果显示
unavailable 与原因，不生成演示曲线。Model Lab 的 dry run 只验证编排并标记为 Demo，不能
进入 Arena 或计入正式结论。

### 快速开始（Web UI）

```bash
# 1) 首次使用：构建 5 个内置真实小数据集（可选，已预装 bt_datasets DB 行则跳过）
backend/.venv/bin/python scripts/build_real_datasets_from_v9.py

# 2) 启动后端 + 前端
./start.sh
# 或分别：backend/.venv/bin/uvicorn app.main:app --port 8000 --reload
#         cd frontend && npm run dev   # http://localhost:5173

# 3) 浏览器打开 → 顶部 Sidebar「回测」或直接访问事件模型页
#    http://localhost:5173/backtest/event
```

典型工作流：先在数据管理页登记并冻结数据 → 在事件模型或量化模型页创建实验 → 到运行记录
查看独立任务进度与详情 → 将完成且可复现的正式结果加入对应 Arena 赛道。跨 API 的 Pronoia
能力评测从 Model Lab 创建批次，并在同一页面查看图表、表格与导出文件。

### 内置 5 个真实小数据集（v9_1000 官方池分层抽样 · 各 10 条 · 100% 真实链接）

| dataset_id | 名称 | 市场 × 类型 | 原文来源 | Oracle T+3 | 日期范围 |
|---|---|---|---|---|---|
| `cn_earnings_10` | CN A股财报业绩预告 10例 | CN × 财报超预期/不及预期 | 东方财富公告链接 | ✅ up/down/neutral | 2025-01 ~ 2026-06 |
| `cn_pure_ma_10` | CN A股并购/资产重组 10例 | CN × 并购/分拆/再融资 | 东方财富公告链接 | ✅ | 2025-03 ~ 2026-06 |
| `cn_guidance_10` | CN A股公司业绩指引 10例 | CN × 公司指引上调/下调 | 东方财富公告链接 | ✅ | 2025-04 ~ 2026-05 |
| `us_sec_ma_10` | US 美股 SEC 并购/分拆申报 10例 | US × 并购/分拆/再融资 | Yahoo Finance SEC Filing | ✅ | 2024-02 ~ 2026-04 |
| `cross_market_mix_10` | 跨市场精选混合 10例 | CN 8 + US 2（均衡） | 全部 http 真实链接 | ✅ | 2025-02 ~ 2026-06 |

严格约束（与 `docs/20260729_design.md` 第 5 节对齐）：
- **零杜撰、零未来日期**：所有 event_time < 发布当天，字段 100% 拷贝自 v9_1000
- **T+3 ACC 的 Wilson 95% CI 下界 ≥ 70%**（系统目标红线）
- **多 horizon 主证据**：默认 `--primary-oracle-horizon avg_all`（T+3/7/15/30/60 加权平均），
  避免短期方差过大；consensus66 作为保守参考（要求 ≥4/5 窗口同向）
- 基准路由：XLK 成分股（AAPL/MSFT/NVDA）→ XLK；QQQ 成分股（AMZN/NFLX/META）→ QQQ；
  其他美股 → SPY；A 股 → 沪深 300（SH000300）；港股 → 恒生指数（HSI）
- `FEVER_BT_STRICT_AS_OF=1` 默认开启：event_study_skill 仅返回事件发生前可用的数据，杜绝前视偏差
- **backtesting/ 目录**：1000 条全量回测数据集 v1（`events_cn_us_1000_v1.jsonl` +
  `labels_cn_us_1000_v1.jsonl`），详见 [backtesting/README.md](backtesting/README.md)

## 🗺 路线图

工作台是 TTRL（Test-Time Reinforcement Learning）的产品地基：当输入、证据、结论、
复盘都被结构化记录后，接入「预测命中评估 → reward 计算 → calibration 更新 →
skill/prompt 策略更新」的长期自进化闭环。

- [x] P0 对话式研究闭环（提问→采证→产出物→Case 沉淀）
- [x] P0 事件研究法引擎（AR/CAR）
- [x] P0 统一回测 Web 平台：事件模型 / 量化模型 / 运行记录 / Model Lab / 数据管理五个独立页面 + Orchestrator + SSE 实时进度 + 暂停/继续/取消 + 事件详情 + 5×10 真实数据集（零杜撰/零未来/真实链接）
- [x] P0 多 horizon 标签体系：T+3/7/15/30/60 CAR + avg_all 加权平均 + consensus66 一致性 + Strict/Lenient 双口径 ACC + 12 horizon 指标 + confidence 分桶校准
- [x] P0 市场分析 Skill 矩阵：6 个 analyzer（公告分类 / AR 分解 / 漂移分析 / CN MA / CN 财报 / US MA），按 market × event_type 路由替代一刀切评分卡
- [x] P0 团队研究上下文共享：ResearchContext 模块，多专家 fan-out 阶段共享研究线索
- [x] P0 1000 条全量回测数据集 v1：backtesting/ 目录独立维护，含使用说明文档
- [x] P0 **可插拔指标系统（Metrics Registry）**：@metric 装饰器注册，列表页指标列动态开关
- [x] P0 **Pronoia 多模型评测（Model Lab）**：多候选 API 预测 + 可选并行问答；独立裁判或人工八维评分；页面图表/表格与 Markdown、JSON、CSV 导出
- [x] P0 **Arena 双赛道横向比较**：事件预测能力 / 投资表现；投资表现支持交集、并集和手动真实时间对齐；显示原生与对齐指标、覆盖警告，候选仅限已完成且可复现的正式 Run
- [x] P0 **成本/效果二维散点（帕累托边界）**：tokens / USD / 耗时 × 准确率 / 综合得分；非支配排序 → 前沿线 + L 型参考线 + 数据表
- [ ] P1 研究资产化：历史 Case 检索、证据有效性标注、复盘面板
- [ ] P1 事件监控与预警（定时任务 + 推送）
- [ ] P2 TTRL v0：命中率统计、calibration 面板
- [ ] P2 接入 Argus 深度采证引擎（见 v2 仓库归档）

## 📋 更新日志

- **3.12.0** · 2026-08-21 · 功能：新增对话历史上下文继承（追问沿用前序Case）、对话失败/暂停醒目标识与一键重试卡片、Team模式4处静默阻塞点实时进度提示、回测中心重构（可插拔多指标分级展示、自动发现数据集、Arena横向比对、成本效果帕累托图）
- **3.11.0** · 2026-08-20 · 功能：新增可插拔指标系统（18+ 指标 core/extended/breakdown 三级、列表页指标列动态切换）；新增 Arena 横向比对平台（同数据集多 Run 360° 评测：排名/雷达/显著性检验/事件级 H2H）；新增成本·效果二维散点图 + 帕累托边界分析（非支配排序 + L 型参考线，X/Y 轴可切换）；修复回测详情页 compat 层老数据无数值（acc/k/n/Wilson 从 bt_runs 标量字段 fallback）；Arena 创建候选仅收录已完成（status=done + done_events>0）的回测 Run
- **3.10.0** · 2026-08-19 · 功能：多 horizon CAR 标签（T+3/7/15/30/60 + avg_all 加权平均）+ 6 个新 analyzer Skill（announcement_classifier / ar_decomposer / drift_context_analyzer / cn_ma_analyzer / cn_earnings_analyzer / us_ma_analyzer）+ Strict/Lenient 双口径 + 12 horizon ACC + confidence 分桶校准 + research_context 团队上下文共享 + backtesting/ 数据集目录（v1）
- **3.9.0** · 2026-08-16 · 功能：新增 Pronoia 回测 Web 平台 P0（全栈）：Data list 选择真实数据集 → 启动/暂停/继续/取消、SSE 实时进度 + 3s 轮询兜底、事件目录 N 条待执行/执行中/已完成、单 Case 详情 6 个 Tab（Team Log/决策结论/Agent 逻辑链/行情视图/As-of Packet/Team Prompt）、原文链接真实可点击。5 个内置真实小数据集各 10 条，全部来自 v9_1000 官方回测池，零杜撰、零未来日期、100% 真实东方财富/Yahoo SEC 原文链接 + 真实 T+3 行情 Oracle Label。
- **3.8.3** · 2026-08-05 · 修补：新增 Pronoia CLI（含 ./p 简写）并增强首页推荐超时兜底
- **3.8.2** · 2026-07-29 · 修补：品牌更名为 Pronoia，并统一首页推荐与团队研究体验
- **3.8.1** · 2026-07-19 · 修补：SkillsTab「对外技能」SectionHeader 改为卡片化标题块（jade 边框 + jade-soft 背景 + 数量徽章 + Agent 实际可调用 hint），视觉权重对齐三层模型 / 底层工具；删除 SectionHeader 死代码。
- **3.8.0** · 2026-07-19 · 功能：三层调度模型对齐：composite skill 改名为 skill（atomic 工具 = tool，对 LLM 不可见；skill 聚合多 atomic，对 LLM 可见；agent 只看 skill）。composite.py → skill.py。9 个 skill：event_study_skill / evidence_graph / financial_research / holder_research / macro_intel / market_research / news_intel / post_market_outlook / stock_overview。前端 SkillsTab 三层模型图示同步：tool(52) → skill(9) → agent(5) → team；CompositeSkillCard 改名为 SkillCard；types.ts 同步更新 category 类型。
- **3.7.0** · 2026-07-19 · 功能：补全 3 个 skill 的美股分支：financial_research 走东财三大报表+财务指标+财报日历+雪球简介+yfinance 卖方研报；holder_research 走 yfinance 股东结构+内部人交易；market_research 在日K 之外追加美股实时行情和公司简介。新增 2 个 atomic tool：get_us_stock_holder（major/institutional/mutualfund holders+insider transactions）、get_us_stock_analyst（recommendations_summary+analyst_price_targets+earnings_estimate+earnings_history）。端到端 NVDA 实测：71% 机构持股、BlackRock 7.96% 持仓、61 位分析师看多、目标价均值 $302.31。
- **3.6.0** · 2026-07-19 · 功能：美股信息查询补全：新增 7 个 atomic tool（实时行情/公司简介/三大报表/财务指标/个股新闻/财报日历/SEC 文件），通过 stock_overview 和 news_intel 暴露给 LLM；search_stock 加 ticker 强信号修复 NVDA 错配高伟达 bug；接 yfinance 提供 Yahoo Finance 个股新闻 + SEC 8-K/10-Q 原文；前端 LogicCard 订阅 store 修复消息流按钮不刷新、GraphView 列表默认 + 节点排序、隐藏 internal 工具、删除冗余「仅深度研究」按钮。
- **3.5.0** · 2026-07-19 · 功能：美股支持补全：财务摘要/指标 K线派生、event_study/market_research/stock_overview 接受 ticker、search_stock 双路并查、_US_NAME_MAP 扩到 250 条
- **3.4.2** · 2026-07-19 · 修补：3.4.2: API 接口清理 — 移除前端 api.health/api.hotTopics（无人调用）；移除后端 /api/hot_topics 端点 + _build_hot_topics 热点缓存（前端已改用静态池）；修正 api.pinArtifact 返回类型 (Artifact) 与后端一致；SSEEvent 新增 team_members 字段以匹配后端 meta 事件。
- **3.4.1** · 2026-07-19 · 修补：3.4.1: 右栏展开态改为 absolute 浮层（z-30 + 左侧投影），不再 shrink-0 挤占聊天区布局。App.tsx 把 ChatPanel + RightPanel 套进 relative 容器让 absolute 生效。
- **3.4.0** · 2026-07-19 · 功能：3.4.0: 空态推荐改为 6 条（2 快速 + 2 专家 + 2 团队），顺序固定；「换一批」改为纯前端静态池洗牌（Fisher-Yates），瞬间完成（< 1ms），不再调后端。Agent 推荐自带 agent 字段，直接走单专家模式。修复 CHIP_PROMPTS 误写 ] 应为 \u007D 的语法错误。
- **3.3.1** · 2026-07-19 · 修补：3.3.1: 空态 hero 调整 — 「换一批」按钮从顶部下移到建议问题下方居中；移除「热点来源」提示行；「团队」徽章从描述行右侧移到左侧图标下方。
- **3.3.0** · 2026-07-19 · 功能：3.3.0: 左栏支持折叠为 w-11 细栏，与右栏对称。展开态 header 右上角加「◀」折叠按钮；折叠态保留 logo 缩写 + 新研究 + 案例计数 + 底部 tab 入口（技能/团队/逻辑库）。状态持久化到 localStorage。
- **3.2.5** · 2026-07-19 · 修补：3.2.5: 能力 chips 去掉 <标的> 占位符，改为从 15 只热门 A 股池随机抽一只填入示例 prompt（宏观类无标的保持原状）。
- **3.2.4** · 2026-07-19 · 修补：3.2.4: 能力 chips 改为可点击按钮，点击后通过 promptSeed 把对应技能的 prompt 模板填到 composer 的 textarea 并自动 focus；新增 store.promptSeed 作为跨组件通道。
- **3.2.3** · 2026-07-19 · 修补：3.2.3: 模式选项从「快速问答 / 单 Agent / 深度研究团队」缩短为「快速 / 专家 / 团队」；右侧 hint span 移除，原信息融合到各模式 placeholder 中。
- **3.2.2** · 2026-07-19 · 修补：3.2.2: 右栏 4 个 tab 改为 flex-1 等宽分布；激活态边框改为 transparent 兜底避免宽度跳变；缩小内边距和字号。
- **3.2.1** · 2026-07-19 · 修补：3.2.1: 逻辑库筛选 chip 改为可换行 + 加内/外间距，字不再挤。
- **3.2.0** · 2026-07-19 · 功能：team 模式新增可勾选团队成员：默认全选、可选择性去掉非 deep_researcher 专家；后端 run_team 支持 team_members 白名单，hard rule 保 deep_researcher 始终参与。
- **3.1.2** · 2026-07-19 · 修补：Agent 选择改用对话框（搜索 + 键盘导航 + 详情预览）；触发器缩小为 chip。
- **3.1.1** · 2026-07-19 · 修补：右栏默认折叠 + UI 状态持久化；单 Agent 模式下拉独立成行避免遮挡；新增版本自动管理脚本 `scripts/bump.py`。
- **3.1.0** · 2026-07-19 · 功能：单 Agent 模式直接调度专家、事件预测员（predictor）Agent 与 post_market_outlook 复合技能、产出物按类型分组、版本自动管理（`scripts/bump.py`）。
- **3.0.0** · 2026-07-18 · 重大：四层调度模型（Tool → Skill → Agent → Team）落地；akshare 真实数据接入；证据图与深度研究团队上线；研究逻辑库（Logic Library）闭环。

## ⚠️ 免责声明

本项目仅供学习与研究使用，所有输出不构成任何投资建议。
数据来自 akshare 免费公开接口，准确性以原始数据源为准。

## 📄 License

MIT
