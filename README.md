# Pronoia

<img width="256" height="256" alt="logo-pronoia" src="https://github.com/user-attachments/assets/88b96dff-7f7b-43aa-aba3-1c4b7c8cbd09" />

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
- **实验性多智能体情景推演**：从证据图异步生成参与方行动与未来情景分支，支持动态
  角色预算、运行前预览、进度恢复和安全取消；coverage-balanced compiler 的 20 例扩大
  留出验证把有效决策角色覆盖率从 76.7% 提升到 100%，遗漏角色从 31 个降到 0；当前仅
  输出未校准情景。
- **密钥安全**：LLM Key 只存后端 `.env`，绝不下发浏览器。
- **🧪 事件驱动回测平台（P0 Web）**：基于真实事件 + 真实行情做系统级回测，
  评估 Tool / Skill / Agent / Team 四层的方向命中率，形成「预测命中 → reward 计算 →
  calibration 更新 → 策略自进化」的可追溯闭环。详见下方独立章节。
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
- **🏟 Arena 横向比对平台**：从同一数据集的多个回测 Run（不同 Runner / Prompt / LLM）
  中一键选择多个 Run，生成**同一数据、不同 Agent 不同 LLM 的 360° 横向评测**——
  综合排名、6 指标雷达图、两两显著性检验（Wilson diff + Fisher 精确检验 + Bonferroni 校正）、
  事件级头对头（Head-to-Head）胜场统计。**Arena 只收录「已完成」的 Runs**，无效/未跑结果不会出现在候选里。
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
                            Ark LLM (OpenAI 兼容) · akshare 数据源
```

## 📁 目录结构

```
pronoia/
├── backend/                # FastAPI 后端主应用（app/ + tests/）
│   └── app/
│       ├── agents/         # Agent 团队（team / evidence_navigator / roster）
│       ├── skills/         # 85+ 数据技能（akshare 行情/新闻/财务/事件研究…）
│       ├── event_backtest/ # 事件驱动回测引擎（collector / labeller / metrics / arena）
│       └── routes/         # REST 路由
├── frontend/               # React 18 + Vite + Tailwind 前端
├── pipelines/              # 运营数据管线脚本（按子域分层）
│   ├── labeling/           # d2 标注 / d3 正文补齐 / d3b 缓存清理 / dx 环境诊断
│   ├── forward_claims/     # 前向断言生成 / 结算 / 验证 + Team 批量推理
│   ├── today/              # 今日事件收集 / 断言验证 / 汇总
│   └── collect/            # 前向事件池采集
├── scripts/                # 仓库级工具脚本（build_doc / start_d5 / ssh_helper …）
├── backtesting/            # 回测数据集 + RLVR 训练/评估 + Judge AB
├── pronoia_run/            # 线上推理工作区（数据快照 / 轨迹 / 运维脚本）
├── data_snapshots/         # 已入库的数据快照（如 today/）
├── docs/                   # 设计文档（含 xml/ 案例上下文）
├── archive/                # 归档的临时/一次性产物
├── pronoia · p · start.sh  # 入口（CLI / 开发启动）
└── README.md · LICENSE · Dockerfile · .env.example
```

> 根目录只放入口与配置；所有可执行脚本按职责归入 `pipelines/` 或 `scripts/`。

## 🚀 快速开始

```bash
cp .env.example .env        # 填入你的 ARK_API_KEY（或任意 OpenAI 兼容端点）
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
docker build -t fever . && docker run -p 8000:8000 fever
# 打开 http://localhost:8000
```

多智能体事件推演目前通过可选的独立网关接入，不影响 Pronoia 的默认启动和其他功能。
接口边界、配置和 Draft 阶段的交付范围见
[MiroFish 多智能体事件推演](docs/mirofish-simulation.md)。32 次 4/6/8 角色规模实验的
冻结数据、结果图和离线复算入口见
[多智能体规模实验 v1](backtesting/mirofish_actor_scale_v1/)。

## 🧭 两种模式

| 模式 | 适用 | 链路 |
|---|---|---|
| ⚡ 快速问答 | 单一事实/单一标的查询 | 主理人 Agent + ≤8 轮工具循环 |
| 👥 深度研究团队 | 多维度深度问题 | Planner 拆 2~5 子任务 → 最多 5 位专家串行 → 证据图 → 综合与复核 |

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

## 🧪 Pronoia 事件驱动回测平台（P0 · Web + CLI 双入口）

基于**真实事件 + 真实行情**的系统级回测。把每一次 Team/Predictor 的预测、
Agent 协作的推理链、Tool/Skill 调用参数与结果，与 Oracle T+3 超额收益（CAR）
放在同一面板对比，用于量化评估 **Tool → Skill → Agent → Team** 四层的方向命中率，
形成「预测命中评估 → reward 计算 → calibration 更新 → 策略自进化」的可追溯闭环。

### 五层架构（含 Arena 横向比对）

```
┌─────────────────────────── Frontend (React 18 · Vite · /backtest · /arena) ─────────────────────┐
│  BacktestList: 选数据集 → 创建 run                          ArenaList: 选 Runs → 创建 Arena    │
│       ↓ 详情 BacktestDetail: 6 Tabs（Log/决策/逻辑链/行情/                           ↓ 详情     │
│  Zustand store ← api.ts ← REST POST/GET ─┐          ArenaDetail: 排名/雷达/显著性/H2H/成本·效果 │
│                                           │          (帕累托边界散点图 + 前沿线)                 │
└───────────────────────────────────────────┤                                                  │
│  GET /api/bt/runs · /metrics · /datasets · Backend routes: backtest.py + arena.py               │
│  FastAPI  ├─ schemas.py: + BTArena / ArenaComputeResult / ParetoChartPoint                      │
│  (cont)   ├─ event_backtest: arena.py (compute_arena_result) · metrics_registry.py (@metric)    │
│           │              成本聚合: tokens_in/out · step_ms_total; 非支配排序 → 帕累托前沿       │
│           └─ db.py: 新增 bt_arenas + bt_metrics_snapshots 表                                    │
│               bt_runs / bt_predictions / bt_datasets / bt_sse_broadcasts / bt_locks  → 共 7 表  │
└───────────────────────────────────────────┬──────────────────────────────────────────────────────┘
                                            ▼
                     真实数据源：akshare (CN/HK) · yfinance (US) · 巨潮/东财公告原文
```

### 回测平台核心能力清单
- **可插拔指标**：`backend/app/event_backtest/metrics_registry.py` 用 `@metric` 装饰器注册，
  每个指标独立的 `compute()`、`to_dict()`、display_name、tier（core/extended/breakdown）、
  higher_is_better。列表页指标列芯片「A T3 准确率 / B 主口径 / 重置」可动态开关，
  支持未来接入自定义打分器。
- **Arena 横向比对**：从「同一数据集的已完成回测 Run」中多选 → 一键生成
  `BTArena` 比对任务。`event_backtest/arena.py::compute_arena_result` 输出：
  综合排名（rank_average 倒数越大越优）、6 指标雷达图、pairwise_tests 显著性矩阵
  （Wilson score diff + Fisher exact + Bonferroni 多重比较校正）、
  events_head_to_head 事件级胜场。详情页 5 个 Tab：概览排名 / 雷达图 / 显著性检验 /
  头对头 / **成本·效果（帕累托）**。
- **帕累托边界**：成本字段 = per-step tokens_in/out 聚合 + USD 估算 + step_ms；
  效果字段 = 主口径 acc 或 综合 composite_score；对所有 Run 做非支配排序
  （A 支配 B ⇔ 效果≥B 且 成本≤B 且 至少一项严格更优）得到前沿集，
  成本最低者向原点延伸 L 型参考线，散点图 + 虚线前沿 + 数据表格，
  切换 X/Y 轴时前端重算前沿并立即更新显示。

### 快速开始（Web UI）

```bash
# 1) 首次使用：构建 5 个内置真实小数据集（可选，已预装 bt_datasets DB 行则跳过）
backend/.venv/bin/python scripts/build_real_datasets_from_v9.py

# 2) 启动后端 + 前端
./start.sh
# 或分别：backend/.venv/bin/uvicorn app.main:app --port 8000 --reload
#         cd frontend && npm run dev   # http://localhost:5173

# 3) 浏览器打开 → 顶部 Sidebar「回测」或直接访问
#    http://localhost:5173/backtest
```

工作流：选数据集（Data list 下拉） → 选 Runner（team_prompt / team_full）与并发 →
点「创建并启动」 → 左侧 run 列表看进度 → 点进详情看 SSE 实时进度、暂停/继续/取消、
事件目录（N 条 pending/processing/done） → 展开单 Case 看 6 个 Tab，
「原文链接」直接 `<a target="_blank">` 跳东方财富 / Yahoo Finance SEC 原文。

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
- [x] P0 事件驱动回测 Web 平台：Backtest 路由 + 5 张 SQLite 表 + Orchestrator + SSE 实时进度 + 暂停/继续/取消 + 事件目录 + 单 Case 6 Tab 详情 + 5×10 真实数据集（零杜撰/零未来/真实链接）
- [x] P0 多 horizon 标签体系：T+3/7/15/30/60 CAR + avg_all 加权平均 + consensus66 一致性 + Strict/Lenient 双口径 ACC + 12 horizon 指标 + confidence 分桶校准
- [x] P0 市场分析 Skill 矩阵：6 个 analyzer（公告分类 / AR 分解 / 漂移分析 / CN MA / CN 财报 / US MA），按 market × event_type 路由替代一刀切评分卡
- [x] P0 团队研究上下文共享：ResearchContext 模块，多专家 fan-out 阶段共享研究线索
- [x] P0 1000 条全量回测数据集 v1：backtesting/ 目录独立维护，含使用说明文档
- [x] P0 **可插拔指标系统（Metrics Registry）**：@metric 装饰器注册，列表页指标列动态开关
- [x] P0 **Arena 横向比对平台**：同一数据集多 Run 一键比对；综合排名 / 6 指标雷达图 / 显著性矩阵 / H2H 事件级胜场；候选仅限已完成 Run
- [x] P0 **成本/效果二维散点（帕累托边界）**：tokens / USD / 耗时 × 准确率 / 综合得分；非支配排序 → 前沿线 + L 型参考线 + 数据表
- [ ] P1 研究资产化：历史 Case 检索、证据有效性标注、复盘面板
- [ ] P1 事件监控与预警（定时任务 + 推送）
- [ ] P2 TTRL v0：命中率统计、calibration 面板
- [ ] P2 接入 Argus 深度采证引擎（见 v2 仓库归档）

## 📋 更新日志

- **3.12.1** · 2026-09-05 · 修补：代码整理：运营管线脚本归入 pipelines/（labeling/forward_claims/today/collect）、today_data→data_snapshots、docxml→docs/xml、tmp_work→archive，统一路径为基于 __file__ 自动定位
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
