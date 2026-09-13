# 更新日志

## 3.13.0 · 2026-09-13

- 合入 main 最新代码：Arena 工作区与模型对比、market_runtime 行情运行时、benchmark 基准解析、launch 启动脚本体系
- 仓库治理：密钥全部改为环境变量读取；运行时数据/回测产物退出版本控制；目录按开源标准重组（scripts/ docs/ share/ data/samples/）
- 修复 vite 构建缺少 @types/node

## 3.12.1 · 2026-09-05

- 代码整理：运营管线脚本归入 pipelines/（labeling/forward_claims/today/collect）、today_data→data_snapshots、统一路径为基于 __file__ 自动定位

## 3.12.0 · 2026-08-21

- 对话历史上下文继承（追问沿用前序 Case）、失败/暂停醒目标识与一键重试卡片、Team 模式 4 处静默阻塞点实时进度提示、回测中心重构（可插拔多指标分级展示、自动发现数据集、Arena 横向比对、成本效果帕累托图）

## 3.11.0 · 2026-08-20

- 可插拔指标系统（18+ 指标 core/extended/breakdown 三级、列表页指标列动态切换）
- Arena 横向比对平台（同数据集多 Run 360° 评测：排名/雷达/显著性检验/事件级 H2H）
- 成本·效果二维散点图 + 帕累托边界分析（非支配排序 + L 型参考线，X/Y 轴可切换）
- 修复回测详情页 compat 层老数据无数值；Arena 创建候选仅收录已完成 Run

## 3.10.0 · 2026-08-19

- 多 horizon CAR 标签（T+3/7/15/30/60 + avg_all 加权平均）
- 6 个新 analyzer Skill（公告分类 / AR 分解 / 漂移分析 / CN MA / CN 财报 / US MA）
- Strict/Lenient 双口径 + 12 horizon ACC + confidence 分桶校准
- research_context 团队上下文共享 + backtesting/ 数据集目录（v1）

## 3.9.0 · 2026-08-16

- Pronoia 回测 Web 平台 P0（全栈）：真实数据集 → 启动/暂停/继续/取消、SSE 实时进度、事件目录、单 Case 6 Tab 详情、真实原文链接

## 3.8.x · 2026-07-29 ~ 08-05

- 3.8.3 新增 Pronoia CLI；3.8.2 品牌更名为 Pronoia；3.8.1 SkillsTab 卡片化

## 3.7.0 ~ 3.0.0 · 2026-07-18 ~ 07-19

- 3.7.0 美股 skill 分支补全（财务/股东/行情 + yfinance 研报）
- 3.6.0 美股信息查询补全（7 个 atomic tool + SEC 原文）
- 3.5.0 美股支持补全（search_stock 双路并查）
- 3.4.x API 接口清理与右栏布局优化
- 3.3.0 左栏折叠为细栏，状态持久化
- 3.2.x 能力 chips 可点击、模式名称简化
- 3.1.x Agent 选择对话框、版本自动管理脚本 scripts/bump.py
- 3.0.0 四层调度模型（Tool → Skill → Agent → Team）落地；akshare 真实数据接入；证据图与深度研究团队上线
