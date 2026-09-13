# 回测数据集使用说明

本目录存放回测研究代码与数据集规范。**数据文件不进入版本库**：1000 条全量事件池、
Oracle 标签、轨迹 checkpoint 与实验产物均为本地数据，由 `.gitignore` 排除；
仓库内仅跟踪 `data/samples/` 下的 8 个小样本（每市场 10 条，用于格式示例与冒烟测试）。

## 数据文件（本地，不入库）

| 文件 | 说明 |
|---|---|
| `events_cn_us_1000_v1.jsonl` | 事件池 v1（CN+US 1000 条），每行一个 JSON 事件记录 |
| `labels_cn_us_1000_v1.jsonl` | Oracle 标签 v1（CN+US 1000 条），含 T+3/7/15/30/60 + avg_all 多 horizon 方向标签 |
| `_trajectory_ckpt_*/` | 轨迹 checkpoint（断点续跑用，可删除重建） |
| `mirofish_*/` | 多智能体情景推演实验产物 |
| `README.md` | 本说明文档 |

> 需要重建数据集时，使用 `scripts/build_real_datasets_from_v9.py` 与 Labeller CLI 重新生成。

**当前版本：v1**（首版正式数据集 + avgCAR 多 horizon 标签体系）

**市场标识 `cn_us`**：CN（A 股 700 条）+ US（美股 300 条）

## 数据集概况

| 项 | 值 |
|---|---|
| 事件总数 | 1000 |
| 时间跨度 | 2024-01-01 ~ 2026-06-22 |
| 标签体系 | avgCAR（多 horizon 加权平均，主证据 = `avg_all`） |
| 抽样方式 | 按市场 × 事件类型分层均衡抽样 |

### 按市场分布

| 市场 | 数量 | 占比 |
|---|---|---|
| CN（A 股） | 700 | 70% |
| US（美股） | 300 | 30% |

### 按事件类型分布

| 事件类型 | 数量 |
|---|---|
| 并购/分拆/再融资 | 220 |
| 财报超预期/不及预期 | 220 |
| 公司指引上调/下调 | 140 |
| 政策利率调整 | 140 |
| 增长/就业数据意外 | 140 |
| 通胀数据意外 | 140 |

### 按市场 × 事件类型

| 市场·事件类型 | 数量 |
|---|---|
| CN·并购/分拆/再融资 | 154 |
| CN·财报超预期/不及预期 | 154 |
| CN·公司指引上调/下调 | 98 |
| CN·政策利率调整 | 98 |
| CN·通胀数据意外 | 98 |
| CN·增长/就业数据意外 | 98 |
| US·并购/分拆/再融资 | 66 |
| US·财报超预期/不及预期 | 66 |
| US·政策利率调整 | 42 |
| US·增长/就业数据意外 | 42 |
| US·通胀数据意外 | 42 |
| US·公司指引上调/下调 | 42 |

## events_cn_us_1000_v1.jsonl 字段说明

每行一个 JSON 对象，字段如下：

| 字段 | 类型 | 说明 |
|---|---|---|
| `event_id` | string | 事件唯一 ID，格式 `seed_<market>_<symbol>_event_<hash>` |
| `market` | string | 市场代码：`CN` / `US` |
| `symbol` | string | 标的代码（A 股 6 位代码 / 美股 ticker / ETF 代码） |
| `event_time` | string | 事件时间，ISO 8601 日期（`YYYY-MM-DD`） |
| `event_type_l2` | string | 事件二级分类（见上表 6 类） |
| `title` | string | 事件标题（公告/数据公布标题） |
| `event_text` | string | 事件正文（公告摘要或数据描述） |
| `source_url` | string | 信息源 URL |
| `sector_etf` | string | 关联行业 ETF（可选） |
| `benchmark` | string | 基准指数（A 股 `sh000300` / 美股 `SPY` `QQQ` `XLK` 等） |
| `direction_prior` | string | 方向先验（`up` / `down` / 空） |
| `event_strength` | string | 事件强度标记 |

### 样例（首条）

```json
{
  "event_id": "seed_cn_600539_event_1386137ffb",
  "market": "CN",
  "symbol": "600539",
  "event_type_l2": "并购/分拆/再融资",
  "event_time": "2025-03-08",
  "title": "...",
  "event_text": "...",
  "source_url": "...",
  "benchmark": "sh000300"
}
```

## labels_cn_us_1000_v1.jsonl 字段说明

每行一个 JSON 对象，与事件池按 `event_id` 一一对应。

### CAR 收益字段

| 字段 | 说明 |
|---|---|
| `car_t1` / `car_t3` / `car_t5` / `car_t7` / `car_t15` / `car_t30` / `car_t60` | T+N 累计异常收益（小数，1.0=100%） |
| `car_tN_pvalue` | 对应 horizon 的统计显著性 p 值 |
| `ret_tN` / `bm_ret_tN` | 个股 N 日收益 / 基准 N 日收益 |

### avgCAR 加权平均字段

| 字段 | 说明 |
|---|---|
| `car_avg_short` | 短期平均（T+3 / T+5） |
| `car_avg_mid` | 中期平均（T+7 / T+15） |
| `car_avg_long` | 长期平均（T+30 / T+60） |
| `car_avg_all` | **全周期加权平均**（T+3:35% / T+7:28% / T+15:20% / T+30:12% / T+60:5%） |

### 方向标签字段

| 字段 | 取值 | 说明 |
|---|---|---|
| `label_tN` | `up` / `down` / `neutral` | 单 horizon 方向标签（按 CAR 阈值） |
| `label_avg_short` / `label_avg_mid` / `label_avg_long` | 同上 | 对应 avgCAR 的方向 |
| `label_avg_all` | 同上 | **主证据方向标签** |
| `label_consensus66` | 同上 | 多窗口一致性方向（≥66% 窗口同向） |

### 一致性指标字段

| 字段 | 说明 |
|---|---|
| `n_horizons_valid` | 有效 horizon 数 |
| `n_horizons_signed` | 有方向的 horizon 数 |
| `consensus_up_frac` / `consensus_down_frac` | 上涨/下跌窗口占比 |
| `consensus_maj_frac` | 主方向窗口占比 |
| `consensus_net` | 净一致性（-1 到 +1） |

## 基准路由规则

不同市场/标的对应不同基准，避免用错基准导致 CAR 失真：

| 市场 | 标的范围 | 基准 |
|---|---|---|
| CN | 全部 A 股 | `sh000300`（沪深 300） |
| US | XLK 成分股（AAPL/MSFT/NVDA） | `XLK` |
| US | QQQ 成分股（AMZN/NFLX/META） | `QQQ` |
| US | 其他美股 | `SPY` |

## 回测使用方法

### 1. 启动回测

```bash
cd backend
.venv/bin/python -m app.cli bt run \
  --events ../backtesting/events_cn_us_1000_v1.jsonl \
  --out ../backtesting/preds_cn_us_1000_v1.jsonl \
  --run-id tf_cn_us_1000_v1 \
  --runner team_full \
  --concurrency 4 \
  --trajectory-ckpt-dir ../backtesting/_trajectory_ckpt_tf_cn_us_1000_v1 \
  --primary-oracle-horizon avg_all
```

### 2. 评分

```bash
.venv/bin/python -m app.cli bt score \
  --events ../backtesting/events_cn_us_1000_v1.jsonl \
  --preds ../backtesting/preds_cn_us_1000_v1.jsonl \
  --labels ../backtesting/labels_cn_us_1000_v1.jsonl \
  --primary-oracle-horizon avg_all \
  --gate 0.50
```

### 3. 指标说明

- **Strict ACC**：严格准确率，预测方向必须与 Oracle 标签完全一致才算正确
- **Lenient ACC**：宽松准确率，只在双方（预测和 Oracle）都非 neutral 时才计分
- **High Conf ACC**：置信度 ≥0.70 的样本的 Strict ACC，反映模型有把握时的表现
- **Neutral 占比**：预测为 neutral 的比例，目标 15-20%（实际偏高，主要因宏观事件信号缺失）

## Gate 阈值说明

| Gate 阈值 | 用途 | 影响 |
|---|---|---|
| `0.50` | 当前默认 | Neutral 占比 ~60%，Strict ACC ~35% |
| `0.60` | 早期版本 | Neutral 占比偏高（~34%+），误拦截多 |
| `0.70` | 高置信分析 | 仅看模型有把握的样本，ACC 显著提升 |

## 相关脚本

| 脚本 | 路径 | 说明 |
|---|---|---|
| 进度报告 | `data/_progress_report.py` | 实时统计 ckpt 数 + 多分桶指标 |
| 负面 case 分析 | `data/_neg_case_final.py` | 提取错误 case 并按类型归因 |
| Labeller | `backend/app/event_backtest/labeller.py` | 生成多 horizon CAR + avgCAR 标签 |
| Metrics | `backend/app/event_backtest/metrics.py` | 计算 Strict/Lenient ACC + Wilson CI |
| CLI | `backend/app/event_backtest/cli.py` | `bt run` / `bt score` 命令入口 |

## 已知问题

1. **宏观类事件 event_text 为占位符**：通胀/政策利率事件的 `event_text` 多为模板（如 `"MLF | 央行官网 预期:机构 前值:上期"`），无实际数值，导致模型无法判断超预期方向
2. **A 股并购公告分类错位**：部分「监管指引第7号合规自查」被误标为「公司指引上调/下调」事件类型
3. **行情接口失效**：部分 ETF（如 SH516160）在 as_of 模式下行情接口连续失败，导致信号全空
4. **Neutral 占比偏高**：目标 15-20%，实际 ~60%，主要因上述数据源问题导致信号缺失

## 版本

- 数据版本：**v1**
- 数据集来源：首版正式发布（前身为内部 balanced_v9 草案）
- 标签体系：多 horizon 加权平均（avgCAR）
- 生成日期：2026-08-16
- 文档日期：2026-08-19

### 版本历史

| 版本 | 日期 | 主要变更 |
|---|---|---|
| **v1** | 2026-08-16 | 当前版本。首版正式发布。T+3/7/15/30/60 多 horizon + avgCAR 加权平均 + 一致性指标 |
