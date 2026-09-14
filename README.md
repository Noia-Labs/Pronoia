# Pronoia

> **Hunt events. Trace echoes.** — 对话式 AI 金融事件分析工作台：提问即研究。

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)]()
[![React](https://img.shields.io/badge/React-18-61dafb.svg)]()

Pronoia 是开源的对话式金融研究工作台：主理人 Agent 调用真实数据技能（akshare / yfinance），
流式输出结论，并把 K 线、事件研究曲线、数据表与研究报告沉淀为可回看的研究资产。
深度问题可切换「研究团队」模式，多专家 Agent 并行作业、复核员把关。
内置事件驱动回测平台，用真实事件 + 真实行情量化评估 Tool / Skill / Agent / Team 四层的方向命中率。

## 核心能力

- **对话式研究工作台**：Case 持久化（SQLite），刷新不丢，随时回看与追问
- **85+ 真实数据技能**：行情 / 新闻 / 公告 / 财务 / 宏观 / 事件研究法（AR/CAR），零 mock
- **研究团队模式**：Planner 拆解 → 多专家并行 → 综合 → 复核员逐条核对「数据事实 vs 模型推断」
- **产出物体系**：工具结果自动生成图表、数据表、证据卡片，一键生成研究报告
- **事件驱动回测**：真实事件 + Oracle T+3~T+60 CAR 标签，Strict/Lenient 双口径 ACC，Wilson CI
- **Arena 横向评测**：同数据集多 Run 一键比对 —— 排名 / 雷达图 / 显著性检验 / 头对头 / 成本-效果帕累托边界
- **密钥安全**：LLM Key 只存后端 `.env`，绝不下发浏览器

## 快速开始

```bash
cp .env.example .env   # 填入 LLM_API_KEY（或任意 OpenAI 兼容端点）
./start.sh             # 后端 :8000 + 前端 :5173
```

Docker（单容器，后端托管前端构建产物）：

```bash
docker build -t pronoia . && docker run -p 8000:8000 pronoia
```

CLI：

```bash
scripts/pronoia health
scripts/pronoia chat "分析贵州茅台近一个月走势"
scripts/pronoia chat "对英伟达做深度研究" --mode team
scripts/pronoia bt run --events data/samples/events_cn_stock_10.jsonl \
                       --labels data/samples/labels_cn_stock_10.jsonl \
                       --out /tmp/bt_ckpt/ --runner team_full
```

## 架构

```
┌──────────── Frontend (React 18 + Vite + Tailwind) ────────────┐
│  Sidebar · ChatPanel · RightPanel · Backtest / Arena 工作台    │
└──────────────────────────────┬─────────────────────────────────┘
                               │ REST + SSE
┌──────────────────────────────▼─────────────────────────────────┐
│              Backend (FastAPI · 单进程 · SQLite)               │
│  routes/ ──► llm 流式 tool-call 循环 ──► agents/team 协作      │
│  skills/ 85+ 数据技能（akshare / yfinance / sina）             │
│  event_backtest/ 回测引擎 · metrics_registry · arena           │
└──────────────────────────────┬─────────────────────────────────┘
                               ▼
                    LLM（OpenAI 兼容）· 公开数据源
```

## 目录结构

```
├── backend/        # FastAPI 应用（app/ + tests/）
├── frontend/       # React 18 + Vite + Tailwind
├── pipelines/      # 运营数据管线（labeling / forward_claims / today / collect / online）
├── backtesting/    # 回测研究代码与 RLVR 训练（数据不入库，见 .gitignore）
├── scripts/        # 全部运维与开发脚本（启动 / 分享 / 版本 / 数据集）
├── docs/           # 权威文档（架构、团队分享）
├── share/          # 团队分享包资产（macOS 启动器 / 脱敏说明 / 打包模板）
├── data/samples/   # 示例数据集（8 个小样本，仓库中唯一跟踪的数据）
├── start.sh        # 一键启动入口
└── stop.sh         # 停止入口
```

运行时产物（`pronoia_run/`、`data/`、`outputs/`、`archive/`）不入库，统一由 `.gitignore` 排除。

## 文档

- [回测平台架构](docs/backtesting-architecture.md)
- [团队分享部署](docs/team-sharing.md)
- [更新日志](CHANGELOG.md)
- [回测研究区说明](backtesting/README.md)

## 路线图

- [x] 对话式研究闭环（提问 → 采证 → 产出物 → Case 沉淀）
- [x] 事件研究法引擎（AR/CAR）与事件驱动回测平台
- [x] Arena 横向评测与成本-效果帕累托分析
- [ ] 研究资产化：历史 Case 检索、证据有效性标注、复盘面板
- [ ] 事件监控与预警（定时任务 + 推送）
- [ ] TTRL v0：命中率统计与 calibration 面板，策略自进化闭环

## 免责声明

本项目仅供学习与研究使用，所有输出不构成投资建议。数据来自 akshare 等公开接口，准确性以原始数据源为准。

## License

[MIT](LICENSE)
