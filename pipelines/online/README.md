# Online Test 数据库

完整的数据集构建、裁决器、`event_study_skill` 改造与 100 条 A/B 结果见
[`docs/20260910-online-test-pipeline-judge-event-study.md`](../../docs/20260910-online-test-pipeline-judge-event-study.md)。

当前实现把每日公开来源、候选事件、可读 event packet、模型预测、未来标签和反馈放在同一个 SQLite 数据库中。

## 构建

```bash
backend/.venv/bin/python pipelines/online/summarize_week.py \
  --start 2026-09-01 --end 2026-09-07

backend/.venv/bin/python pipelines/online/build_database.py \
  --start 2026-09-01 --end 2026-09-07

backend/.venv/bin/python pipelines/online/build_expanded_catalog.py \
  --db pronoia_run/online_test/online_test.sqlite3 \
  --out-dir pronoia_run/online_test/week_2026-09-01_2026-09-07/expanded_catalog

backend/.venv/bin/python pipelines/online/build_direction_prediction_set.py \
  --db pronoia_run/online_test/online_test.sqlite3 \
  --out-dir pronoia_run/online_test/week_2026-09-01_2026-09-07/direction_prediction_ge70 \
  --threshold 70

# 不读取正文的全目录候选筛选（用于召回/排队，不直接给方向）
backend/.venv/bin/python pipelines/online/build_metadata_prediction_candidates.py \
  --db pronoia_run/online_test/online_test.sqlite3 \
  --out-dir pronoia_run/online_test/week_2026-09-01_2026-09-07/metadata_prediction_ge50 \
  --threshold 50

backend/.venv/bin/python pipelines/online/build_metadata_event_packets.py \
  --db pronoia_run/online_test/online_test.sqlite3 \
  --out-dir pronoia_run/online_test/week_2026-09-01_2026-09-07/metadata_event_packets
```

默认数据库为 `pronoia_run/online_test/online_test.sqlite3`。重复执行采用 upsert，不会重复插入同一来源或事件。

## 数据层次

- `cohorts`：每日冻结批次和漏斗计数。
- `source_records`：未经筛选的来源记录。
- `classified_event_catalog`：所有来源记录的统一大类、目录角色和逐类判断要求。
- `event_candidates`：命中六类事件规则的候选。
- `events`：正文和身份校验通过的 packet，带 `strict/review/exclude/duplicate` 状态。
- `event_sources`：主文件和关联公告。
- `model_runs` / `predictions`：模型版本、prompt 版本、方向和 Evidence Graph。
- `labels`：T1/T3 等未来收益标签；必须在 `available_at` 后写入。
- `feedback`：错误归类、人工复核和改进建议。
- `prediction_event_eligibility`：方向判断准备度、六维得分和硬门槛失败原因。

## 常用队列

```sql
-- 回填数据可用于离线 smoke test
SELECT event_id, market, symbol, event_type_l2, title
FROM v_offline_evaluation_queue
ORDER BY cohort_date, market, symbol;

-- 只有截止时间前冻结的 strict 事件才会出现在正式在线队列
SELECT * FROM v_online_prediction_queue;

-- 需要人工决定是否纳入的边界样本
SELECT event_id, title, evaluation_selection_reason
FROM v_review_queue;

-- 标签生成后查看逐条预测是否正确
SELECT * FROM v_scored_predictions
WHERE horizon = 'T3';
```

正式 Online ACC 只能使用 `v_online_prediction_queue` 中的事件。事后回填事件即使正文完整，也只能用于离线调试，不能改写 `prediction_eligible`。

## 全事件目录与预测分流

`build_expanded_catalog.py` 会将 `source_records` 中的所有记录分类，不再只覆盖原先六类。
它同时生成以下队列：

- `v_prediction_candidate_catalog`：具有潜在方向机制，但仍需补正文和通过硬门槛；
- `v_event_catalog_review_queue`：类别或影响不明确，需要复核；
- `v_evidence_only_catalog`：程序性、摘要或配套材料，只应挂到主事件的 Evidence Graph。

“进入全量目录”不等于“进入 ACC”。真正用于预测的事件仍需通过来源身份、有效时点、
聚合去重、新颖性、逐类关键字段和可交易性检查。
