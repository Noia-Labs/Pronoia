# Online Test 数据集、裁决器与 Event Study 改造总结

更新日期：2026-09-10
目标分支：`dev-3.12`

## 1. 结论摘要

本轮完成了四项可复用工作：

1. 建立每日公告采集、事件目录、方向候选、Event Packet、未来标签和反馈表组成的 Online Test 数据链路。
2. 从 2026-09-01 至 2026-09-07 的 8,171 条公开记录中构造 1,161 条 metadata 候选，并进一步构造 100 条具备秒级发布时间、完整正文和 T1/T3 行情标签的离线回填评测集。
3. 重修固定方向裁决器，取消 confidence 方向闸门，并将空响应、缺字段和分数方向不一致视为可重试错误，不再静默回退为 neutral。
4. 将 `event_study_skill` 接入统一行情路由，支持证券类型识别、provider fallback、个股/基准并发取数、缓存和精确 `prediction_cutoff_at` 截断。

完整 100 条 A/B 中，atomic claim-v2 改善了 Claim 原子化、支持/反证连边与 Missing 管理，但没有稳定提高方向准确率。当前主要瓶颈仍是事件筛选、正文中的可定向信息，以及行情 Skill 的可用率，而不是单独的 Deep Researcher prompt。

> 本轮 100 条是历史公告的严格 as-of 离线回填，不是当日实时冻结后产生的正式 online ACC。报告中使用 “online test” 指数据和运行框架，所有准确率按 offline backfill 解释。

## 2. Online Test 数据链路

### 2.1 架构

入口代码位于 [`pipelines/online`](../pipelines/online/README.md)，数据库结构位于 [`schema.sql`](../pipelines/online/schema.sql)。核心层次为：

```text
每日公开来源
  -> source_records
  -> classified_event_catalog
  -> metadata/event eligibility 筛选
  -> Event Packet + 冻结公告正文
  -> 三个分析专家（并行）
  -> Deep Researcher A/B（锁步证据）
  -> 固定 T1/T3 裁决器
  -> 到期后生成行情标签
  -> predictions / labels / feedback
```

数据库通过 `cohorts`、`source_records`、`classified_event_catalog`、`events`、`event_sources`、`model_runs`、`predictions`、`labels` 和 `feedback` 保存全链路状态。重复构建采用 upsert，不会因重跑重复插入同一来源或事件。

### 2.2 每日采集和事件目录

[`collect_daily.py`](../pipelines/online/collect_daily.py) 负责冻结每日来源；[`build_database.py`](../pipelines/online/build_database.py) 入库；[`build_expanded_catalog.py`](../pipelines/online/build_expanded_catalog.py) 与 [`event_taxonomy.py`](../pipelines/online/event_taxonomy.py) 将所有记录统一为事件大类和三种目录角色：

- `prediction_candidate`：具有独立方向机制，仍需正文和硬门槛验证；
- `review`：含义或影响不明确，需要人工或正文复核；
- `evidence_only`：程序性文件、摘要或配套材料，应挂到主事件的 Evidence Graph，而不应独立计入 ACC。

2026-09-01 至 2026-09-07 共采集并归档 8,171 条：

| 目录角色 | 数量 |
|---|---:|
| Evidence only | 5,066 |
| Review | 1,860 |
| Prediction candidate | 1,245 |
| 合计 | 8,171 |

### 2.3 1,161 条 metadata 候选

[`build_metadata_prediction_candidates.py`](../pipelines/online/build_metadata_prediction_candidates.py) 在不读取正文的条件下，对完整目录进行 metadata 六维打分；门槛为 50 分。8,171 条中入选 1,161 条，市场分布为 CN 1,157、US 4。

| 一级事件类别 | 数量 |
|---|---:|
| 股东持股与控制权 | 372 |
| 融资与债务 | 344 |
| 监管法律与重大风险 | 147 |
| 经营订单与项目 | 129 |
| 并购重组与资产交易 | 66 |
| 回购分红与股本 | 56 |
| 治理人事与激励 | 39 |
| 财务业绩与指引 | 4 |
| 产品研发与许可 | 4 |

这 1,161 条的 `event_text_kind` 均为 `metadata_capsule_not_announcement_body`，因此只能用于召回和排队；在 Event Scout 成功冻结正文前不能直接解释为正式可预测事件。

### 2.4 程序性公告后验清理

本次对 1,161 条记录进行了标题与 metadata 审计，得到：

逐条分类、规则命中原因和宽/严两种净化清单保存在
[`online_test_1161_detailed_classification.xlsx`](../outputs/online_event_classification_20260910/online_test_1161_detailed_classification.xlsx)。

| 处理 | 数量 | 占比 | 用途 |
|---|---:|---:|---|
| 保留 | 739 | 63.7% | 高置信测试集 |
| 待人工复核 | 235 | 20.2% | 宽口径保留，获取正文后再决定 |
| 明确程序性剔除 | 187 | 16.1% | 不作为独立预测事件 |
| 去程序性宽口径 | 974 | 83.9% | 只移除明确程序性记录 |

明确程序性记录主要包括发行/交易配套申报文件 106 条、中介/专业机构配套意见 33 条、内幕交易或合规自查 29 条，以及募集资金日常管理、零变动披露和机械性证券操作等 19 条。

235 条边界事件主要是减持/增持/回购结果 97 条、权益变动报告 45 条、阶段性进展 33 条、问询回复 26 条、担保/质押变化 20 条及其他 14 条。它们不能仅凭标题自动删除，必须检查正文中的金额、比例、控制权、判决或审批增量。

该清理目前是后验审计结果，尚未自动写回 `build_metadata_prediction_candidates.py` 的生产门槛。下一步应将“明确程序性剔除”和“正文复核队列”作为两个独立输出，避免将人工复核样本误删。

### 2.5 精确 100 条评测集

[`build_precise_t1_t3_cohort.py`](../pipelines/online/build_precise_t1_t3_cohort.py) 从 1,161 条候选中按事件类别分层，要求：

- 公告正文抓取成功且证券身份匹配；
- 公告发布时间精确到秒；
- T0 为公告后第一个满足 09:15 cutoff 的实际交易日；
- 同一 `symbol + effective_session` 只保留一条；
- 截至标签日期可完整观察 T1 和 T3；
- 个股和基准行情完整。

为得到 100 条，实际尝试了 159 条；剔除原因如下：

| 原因 | 数量 |
|---|---:|
| 标签截止日尚不可观察 T3 | 31 |
| symbol + effective session 重复 | 18 |
| T1/T3 行情不完整 | 6 |
| 公告正文抓取失败 | 4 |

100 条标签分布为：T1 up/neutral/down = 49/19/32；T3 = 56/14/30。标签明显偏 up，因此三分类 ACC 需要与类别分布、方向分布和配对翻转一起阅读。

## 3. 冻结正文与专家阶段

新增 [`frozen_document.py`](../backend/app/skills/frozen_document.py)，只读取 Event Packet 已冻结的东财公告或 SEC 官方链接，而不是开放网页搜索。它会：

- 校验域名 allowlist；
- 对东财分页正文进行有界重试；
- 校验证券代码或发行人身份；
- 校验来源日期不晚于事件日期；
- 返回正文 hash、来源时间、页数和截断状态。

Event Scout 的工具列表加入 `frozen_announcement_fetch`；如果 packet 是 metadata-only，运行器会在专家 fan-out 前只抓一次正文并将结果共享给三个专家，避免 A/B 两臂重复访问外部来源。

100 条最终运行中：Event Scout 100/100 正常完成，Market Analyst 100/100 正常完成，Fundamental Analyst 99/100 正常完成。专家顶层完成不等于所有内部 Skill 成功，具体行情成功率见第 5 节。

## 4. Deep Researcher A/B 设计

运行器为 [`run_agent_prompt_ab.py`](../pipelines/online/run_agent_prompt_ab.py)，流程为“三个分析专家并行 -> 相同种子 Evidence Graph -> 两版 Deep Researcher 顺序交错运行 -> 同一个固定裁决器”。

- A：`pre_atomic`，来自 `f6ea0f1^` 的历史 Deep Researcher prompt；
- B：`atomic_claim_v2`，要求 Claim 标题原子化，并在 rationale 中保留“事实、比较、反方/限制、验证条件”；
- 两臂复用完全相同的 packet、正文、专家摘要和工具记录；
- Deep Researcher 阶段只允许操作 Evidence Graph，不允许再次访问外部数据；
- 两臂顺序按 event_id 交错，降低服务端时延或限流对单一版本的偏置。

## 5. `event_study_skill` 改造

### 5.1 统一行情路由

[`analysis.py`](../backend/app/skills/analysis.py) 不再自行直接调用单一 AkShare 接口，而是统一通过现有 [`price_data.py`](../backend/app/skills/price_data.py) 获取个股和基准行情。路由支持：

- CN 股票、ETF、指数的显式类型识别；
- 腾讯有界 K 线优先，AkShare 股票、ETF、指数接口 fallback；
- US Yahoo chart 优先，`yfinance` 与 AkShare fallback；
- 统一 OHLCV 字段、日期过滤和 provider 尝试记录；
- 相同基准请求的 K 线缓存；
- 个股与基准通过两个 worker 并发获取，总等待时间接近较慢的一侧。

### 5.2 精确 as-of 截断

新的 `prediction_cutoff_at` 从 Event Packet 注入 `event_study_skill` 并传到内部 `event_study`：

- `as_of=True` 时永远不返回 post-event CAR；
- 如果预测截止点在 T0 收盘前，行情请求在入口处即截断至 T-1；
- T0 的个股涨跌、基准涨跌和 AR 均返回 `null`；
- 只提供 cutoff 前的 pre5/pre20 漂移和 pre5 超额收益；
- 返回 `data_cutoff_date`、`event_day_data_included` 与 asset/benchmark provider 轨迹，便于审计未来函数。

这一修复纠正了此前“统一把 cutoff 设为 T0 09:15，但 Skill 仍使用 T0 收盘价”的时间口径错误。

### 5.3 当前 100 条中的 Skill 表现

最终运行的 100 个事件都触发了 `event_study_skill`，其中 77 个事件至少成功一次。调用级统计：

| 结果 | 次数 |
|---|---:|
| 成功 | 77 |
| Evidence Navigator 外部 Skill 预算耗尽 | 27 |
| 子 Skill 45 秒超时 | 8 |
| provider/SSL/proxy/数据源失败 | 15 |
| 总调用 | 127 |

因此代码路由和时间截断已修正，但行情可用率仍只有 77%。当前剩余问题包括：模型重复调用导致预算耗尽、`requests` 路径仍可能继承无效代理、腾讯/AkShare 的瞬时 SSL 或分块响应失败，以及子 Skill 45 秒超时。

## 6. 裁决器重修

本轮固定裁决器版本为 `scorecard_dual_horizon_no_confidence_gate_v1`。

### 6.1 方向规则

每个 horizon 独立聚合与 `supports/contradicts` 相连的 Claim：强/中/弱信号分别计 3/2/1 分；`up_score - down_score` 为正输出 up、为负输出 down，只有 0:0 或精确抵消才输出 neutral。

confidence 只表达可靠度，不再通过代码阈值将已有 up/down 强制改写成 neutral。信息不完整也不自动等价于 neutral；若存在未被反驳的实质方向 Claim，应给出方向并降低 confidence。

### 6.2 输出可靠性

裁决器现在要求：

- T1/T3 均存在；
- direction 只能是 up/down/neutral；
- `up_score`、`down_score`、confidence 可解析且范围正确；
- direction 必须与净分一致；
- rationale 非空；
- 空响应、JSON 解析失败、缺字段或内部不一致最多重试 3 次；
- 裁决请求串行并至少间隔 1.05 秒，降低网关 burst/rate-limit；
- 已有完整 Evidence Graph 的 checkpoint 可只修复裁决器，不重复运行专家和 Deep Researcher。

最终 200 个 A/B 输出全部具有有效 T1/T3 裁决。其中 49 个结果通过 judge-only resume 路径修复；修复后没有把 API/格式错误伪装成 neutral。

## 7. 完整 100 条 A/B 结果

最终采用修正开盘前 cutoff 的完整运行目录 `agent_ab_precise_t1_t3_100_preopen_cutoff_v3`。早期 `agent_ab_precise_t1_t3_100` 和 `agent_ab_precise_t1_t3_100_event_study_v2` 分别只完成约 54 条和 15–16 条，不作为最终比较。

### 7.1 方向准确率

| 指标 | pre-atomic | atomic claim-v2 | v2 - pre |
|---|---:|---:|---:|
| T1 strict ACC | 25/100 = 25.0% | 26/100 = 26.0% | +1.0 pp |
| T3 strict ACC | 33/100 = 33.0% | 32/100 = 32.0% | -1.0 pp |
| T1 neutral 占比 | 38.0% | 43.0% | +5.0 pp |
| T3 neutral 占比 | 35.0% | 40.0% | +5.0 pp |

配对结果：

| Horizon | 两者都对 | 仅 pre 对 | 仅 atomic 对 | 两者都错 |
|---|---:|---:|---:|---:|
| T1 | 23 | 2 | 3 | 72 |
| T3 | 27 | 6 | 5 | 62 |

样本规模很小，净差仅一条，不能据此宣称任一 prompt 稳定提升 ACC。

### 7.2 Evidence Graph 结构

| 图谱指标（均值） | pre-atomic | atomic claim-v2 | 变化 |
|---|---:|---:|---:|
| Evidence | 7.49 | 7.26 | -0.23 |
| Claims | 2.13 | 3.32 | +1.19 |
| Edges | 12.91 | 12.75 | -0.16 |
| Supports | 6.25 | 7.72 | +1.47 |
| Contradicts | 0.59 | 1.00 | +0.41 |
| Missing | 3.03 | 2.32 | -0.71 |
| Substantive claim rate | 99.0% | 91.0% | -8.0 pp |
| Atomic-title pass rate | 0.0% | 61.48% | +61.48 pp |
| Rationale complete rate | 100.0% | 100.0% | 0.0 pp |
| Audit findings | 1.89 | 1.71 | -0.18 |

atomic claim-v2 明显增加了 Claim、supports 和 contradicts，并减少 Missing，但图谱结构改善没有转化成稳定 ACC 提升。

### 7.3 删除程序性公告后的 ACC

100 条中有 5 条命中本次明确程序性规则，删除后剩 95 条：

| 版本 | T1 原 ACC | T1 去程序性 | T3 原 ACC | T3 去程序性 |
|---|---:|---:|---:|---:|
| pre-atomic | 25.00% | 22/95 = 23.16% | 33.00% | 31/95 = 32.63% |
| atomic claim-v2 | 26.00% | 23/95 = 24.21% | 32.00% | 31/95 = 32.63% |

若进一步暂缓 16 条“进展/减持结果”等边界样本，只保留 79 条高置信事件：

| 版本 | T1 ACC | T3 ACC |
|---|---:|---:|
| pre-atomic | 18/79 = 22.78% | 26/79 = 32.91% |
| atomic claim-v2 | 19/79 = 24.05% | 27/79 = 34.18% |

去程序性后 ACC 没有机械上升，因为被删除的 5 条在 T1 中恰好有 3 条被两个版本正确预测。清洗的价值是提高评测对象的经济含义和可解释性，而不是保证某个小样本上的分数上涨。

## 8. 修改文件索引

| 文件 | 修改内容 |
|---|---|
| [`backend/app/agents/roster.py`](../backend/app/agents/roster.py) | Event Scout 加入冻结正文读取；严格 as-of 文案改为 cutoff 前已完整收盘数据 |
| [`backend/app/agents/team.py`](../backend/app/agents/team.py) | 自动向正文抓取和 event study 注入 packet 中的确定参数与 cutoff |
| [`backend/app/event_backtest/engine.py`](../backend/app/event_backtest/engine.py) | backtest prompt 对齐 09:15 cutoff 和 T-1 行情语义 |
| [`backend/app/llm.py`](../backend/app/llm.py) | LLM 客户端禁用环境代理继承，保留分层 Skill 超时 |
| [`backend/app/skills/analysis.py`](../backend/app/skills/analysis.py) | event study 统一行情路由、并发、缓存、provider 轨迹与 cutoff 截断 |
| [`backend/app/skills/skill.py`](../backend/app/skills/skill.py) | 高层 `event_study_skill` 新增 cutoff 参数并屏蔽未来字段 |
| [`backend/app/skills/registry.py`](../backend/app/skills/registry.py) | 注册冻结公告正文 Skill |
| [`backend/app/skills/frozen_document.py`](../backend/app/skills/frozen_document.py) | 新增 allowlist、身份/as-of 校验和有界正文读取 |
| [`pipelines/online`](../pipelines/online/README.md) | 新增采集、数据库、分类、候选、精确 cohort、标签和锁步 A/B 流程 |
| [`backend/tests/test_event_study_price_routing.py`](../backend/tests/test_event_study_price_routing.py) | 验证行情路由、T-1 截断和基准缓存 |
| [`backend/tests/test_frozen_document.py`](../backend/tests/test_frozen_document.py) | 验证正文 allowlist、身份/as-of 校验和运行器预取 |
| [`backend/tests/test_online_judge_reliability.py`](../backend/tests/test_online_judge_reliability.py) | 验证空响应重试、缺 horizon 和分数方向不一致拒绝 |

## 9. 验证与复现

核心测试：

```bash
PYTHONPATH=. backend/.venv/bin/python -m unittest -v \
  backend.tests.test_external_data_resilience \
  backend.tests.test_event_study_price_routing \
  backend.tests.test_frozen_document \
  backend.tests.test_online_judge_reliability
```

完整 100 条 A/B 的运行入口：

```bash
PYTHONPATH=backend backend/.venv/bin/python pipelines/online/run_agent_prompt_ab.py \
  --events pronoia_run/online_test/week_2026-09-01_2026-09-07/precise_t1_t3_100/events_precise_t1_t3_100.jsonl \
  --labels pronoia_run/online_test/week_2026-09-01_2026-09-07/precise_t1_t3_100/labels_precise_t1_t3_100.jsonl \
  --out-dir pronoia_run/online_test/week_2026-09-01_2026-09-07/agent_ab_precise_t1_t3_100_preopen_cutoff_v3 \
  --event-concurrency 1 --max-rounds 8 --resume
```

## 10. 已知限制和下一步

1. 将明确程序性规则接入 metadata 候选构建，并保留独立的正文复核队列；不要直接删除全部“进展/结果”类公告。
2. 为 `event_study_skill` 增加模型侧幂等约束：每个事件最多一次成功调用；DR 只消费专家产物，避免预算耗尽。
3. 让 `price_data.py` 的直接 HTTP 请求显式禁用无效环境代理，并对 SSL/分块错误实施 provider 级退避和熔断。
4. 将日线信号预计算到 Event Packet，在线阶段优先读冻结行情特征，实时 provider 只作为补充。
5. 正式报告同时给出全量、去程序性、高置信三种口径，以及预测分布、配对翻转和置信区间，避免单一 ACC 掩盖样本构成变化。
