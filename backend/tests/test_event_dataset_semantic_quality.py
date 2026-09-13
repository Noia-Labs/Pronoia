from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestEventDatasetSemanticQuality(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write(self, name: str, rows: list[dict]) -> Path:
        path = self.root / name
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _real_row(event_id: str = "real", event_time: str = "2025-01-06") -> dict:
        return {
            "event_id": event_id,
            "market": "CN",
            "symbol": "600000",
            "event_time": event_time,
            "event_type_l2": "公司公告",
            "title": "公司发布经审计年度报告",
            "event_text": "公司披露归母净利润同比增长 18.2%，报告已由交易所公开。",
            "source_url": "https://example.test/announcement/1",
            "benchmark": "sh000300",
        }

    def test_synth_and_placeholder_are_demo_only_with_auditable_ratios(self) -> None:
        from app.event_backtest.datasets import inspect_event_semantic_quality

        synthetic = {
            **self._real_row("demo", "2025-01-04"),
            "event_text": "PPI | BLS consensus: survey prev: prior",
            "source_url": "synth-v7:inflation:2025Q1",
        }
        quality = inspect_event_semantic_quality(
            self._write("demo.jsonl", [synthetic, self._real_row()])
        )
        self.assertEqual(quality["status"], "demo_only")
        self.assertEqual(quality["synthetic_ratio"], 0.5)
        self.assertEqual(quality["placeholder_ratio"], 0.5)
        self.assertEqual(quality["fact_complete_ratio"], 0.5)
        self.assertEqual(quality["weekend_ratio"], 0.5)
        self.assertFalse(quality["formal_evaluation_eligible"])
        self.assertIn("contains_synthetic_events", quality["reason_codes"])
        self.assertIn("contains_placeholder_events", quality["reason_codes"])

    def test_structural_quality_is_not_relabelled_as_semantic_quality(self) -> None:
        from app.event_backtest.datasets import materialize_contract

        path = self._write("research.jsonl", [self._real_row()])
        contract = materialize_contract({
            "dataset_kind": "event",
            "path": str(path),
            "source": {"type": "local_file", "provider": "official_archive", "ref": str(path)},
            "quality_status": "passed",
        })
        self.assertEqual(contract["quality_status"], "passed")
        self.assertEqual(contract["semantic_quality"]["status"], "research_ready")
        self.assertTrue(contract["semantic_quality"]["formal_evaluation_eligible"])
        self.assertEqual(
            contract["quality_report"]["semantic_quality"],
            contract["semantic_quality"],
        )

    def test_declared_report_cannot_override_scanned_demo_result(self) -> None:
        from app.event_backtest.datasets import materialize_contract

        row = {
            **self._real_row("demo"),
            "event_text": "工业增加值 | 统计局 预期:机构 前值:上月",
            "source_url": "synth-v7:growth:2025Q1",
        }
        path = self._write("declared.jsonl", [row])
        contract = materialize_contract({
            "dataset_kind": "event",
            "path": str(path),
            "quality_status": "passed",
            "quality_report": {
                "semantic_quality": {
                    "status": "research_ready",
                    "formal_evaluation_eligible": True,
                },
            },
        })
        self.assertEqual(contract["quality_status"], "passed")
        self.assertEqual(contract["semantic_quality"]["status"], "demo_only")
        self.assertEqual(contract["quality_report"]["semantic_quality"]["status"], "demo_only")

    def test_incomplete_real_facts_are_partial_but_not_known_demo(self) -> None:
        from app.event_backtest.datasets import inspect_event_semantic_quality

        partial = self._real_row()
        partial["event_text"] = ""
        partial["source_url"] = ""
        quality = inspect_event_semantic_quality(self._write("partial.jsonl", [partial]))
        self.assertEqual(quality["status"], "partial")
        self.assertEqual(quality["fact_complete_ratio"], 0.0)
        self.assertTrue(quality["formal_evaluation_eligible"])

    def test_manual_placeholder_source_is_not_claimed_as_traceable(self) -> None:
        from app.event_backtest.datasets import inspect_event_semantic_quality

        row = self._real_row()
        row["source_url"] = "manual://event/manual-1"
        quality = inspect_event_semantic_quality(self._write("manual.jsonl", [row]))

        self.assertEqual(quality["status"], "partial")
        self.assertEqual(quality["fact_complete_ratio"], 0.0)
        self.assertEqual(quality["traceable_source_ratio"], 0.0)
        self.assertIn("missing_traceable_source", quality["reason_codes"])

    def test_dataset_response_exposes_semantic_quality(self) -> None:
        from app.routes.backtest import _dataset_response

        path = self._write("response.jsonl", [self._real_row()])
        response = _dataset_response({
            "id": "events-real",
            "name": "真实事件",
            "path": str(path),
            "dataset_kind": "event",
            "status": "available",
            "quality_status": "passed",
            "source": {"type": "local_file", "provider": "official_archive", "ref": str(path)},
        })
        self.assertEqual(response.quality_status, "passed")
        self.assertEqual(response.semantic_quality["status"], "research_ready")

    def test_legacy_run_response_backfills_dataset_snapshot_semantics_read_only(self) -> None:
        from app.event_backtest.protocol import build_protocol_hash, events_snapshot_sha256
        from app.routes.backtest import _ensure_protocol_hash

        demo = {
            **self._real_row("legacy-demo"),
            "event_text": "PPI | consensus: survey prev: prior",
            "source_url": "synth-v7:inflation:2025Q1",
        }
        events = self._write("legacy-events.jsonl", [demo])
        labels = self._write("legacy-labels.jsonl", [
            {"event_id": "legacy-demo", "label_t3": "up", "car_t3": 0.01},
        ])
        evaluation = {"evaluation_horizon": "t3"}
        run = {
            "id": "legacy-run",
            "events_path": str(events),
            "labels_path": str(labels),
            "dataset_version": "dsv_legacy",
            "engine_mode": "event_proxy",
            "result_nature": "proxy",
            "execution_spec": {},
            "config": {
                "evaluation_protocol": evaluation,
                "dataset_snapshot": {
                    "snapshot_hash": events_snapshot_sha256(events),
                    "dataset_kind": "event",
                },
            },
        }
        run["protocol_hash"] = build_protocol_hash(
            events_path=events,
            labels_path=labels,
            execution_spec={},
            evaluation_protocol=evaluation,
            dataset_version="dsv_legacy",
            engine_mode="event_proxy",
        )
        enriched = _ensure_protocol_hash(run)
        self.assertNotIn("semantic_quality", run["config"]["dataset_snapshot"])
        self.assertEqual(
            enriched["config"]["dataset_snapshot"]["semantic_quality"]["status"],
            "demo_only",
        )

    def test_manual_dataset_persists_structured_facts_and_pre_event_features(self) -> None:
        from app.routes.backtest import create_manual_dataset
        from app.schemas import CreateManualDatasetRequest

        captured: dict = {}

        def fake_upsert(**kwargs):
            captured.update(kwargs)
            return {
                **kwargs,
                "id": kwargs["dataset_id"],
                "dataset_version": kwargs.get("dataset_version"),
            }

        request = CreateManualDatasetRequest(**{
            "name": "结构化事件",
            "events": [{
                "event_id": "macro-1",
                "market": "US",
                "symbol": "SPY",
                "event_time": "2025-01-06T08:30:00-05:00",
                "available_time": "2025-01-06T08:31:00-05:00",
                "event_type_l2": "通胀数据意外",
                "title": "美国 CPI 公布",
                "event_text": "美国劳工统计局公布 CPI 数据。",
                "source_url": "https://example.test/bls/cpi",
                "event_facts": {
                    "actual_value": 3.2,
                    "expected_value": 3.1,
                    "previous_value": 3.0,
                    "value_unit": "%",
                },
                "pre_event_features": {
                    "asset_return_5d_pct": 1.2,
                    "asset_return_20d_pct": -0.8,
                    "as_of_note": "prior_close_only",
                },
            }],
        })
        with patch("app.routes.backtest.DATA_DIR", str(self.root / "data")), patch(
            "app.routes.backtest.db.new_id", return_value="manual-test"
        ), patch("app.routes.backtest.db.upsert_bt_dataset", side_effect=fake_upsert):
            response = create_manual_dataset(request)

        row = json.loads(Path(captured["path"]).read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["event_facts"]["actual_value"], 3.2)
        self.assertEqual(row["event_facts"]["expected_value"], 3.1)
        self.assertEqual(row["pre_event_features"], {
            "asset_return_5d_pct": 1.2,
            "asset_return_20d_pct": -0.8,
            "as_of_note": "prior_close_only",
        })
        self.assertEqual(response.semantic_quality["status"], "research_ready")

    def test_formal_event_arena_rejects_demo_but_exploration_is_explicit(self) -> None:
        from fastapi import HTTPException

        from app.event_backtest.protocol import build_protocol_hash, events_snapshot_sha256
        from app.routes.arena import _validate_protocols, _validate_scored_runs

        events = self._write("arena-events.jsonl", [self._real_row()])
        labels = self._write("arena-labels.jsonl", [
            {"event_id": "real", "label_t3": "up", "car_t3": 0.02},
        ])
        demo_quality = {
            "status": "demo_only",
            "synthetic_ratio": 1.0,
            "placeholder_ratio": 1.0,
            "fact_complete_ratio": 0.0,
            "weekend_ratio": 0.0,
            "reasons": ["合成测试事件"],
            "formal_evaluation_eligible": False,
        }

        def run(run_id: str) -> dict:
            evaluation = {"evaluation_horizon": "t3"}
            item = {
                "id": run_id,
                "status": "done",
                "events_path": str(events),
                "labels_path": str(labels),
                "dataset_version": "dsv_demo",
                "engine_mode": "event_proxy",
                "result_nature": "proxy",
                "runner": "baseline",
                "strategy_spec": {"type": "event", "runner": "baseline"},
                "execution_spec": {},
                "config": {
                    "evaluation_protocol": evaluation,
                    "dataset_snapshot": {
                        "dataset_kind": "event",
                        "snapshot_hash": events_snapshot_sha256(events),
                        "semantic_quality": demo_quality,
                    },
                },
            }
            item["protocol_hash"] = build_protocol_hash(
                events_path=events,
                labels_path=labels,
                execution_spec={},
                evaluation_protocol=evaluation,
                dataset_version="dsv_demo",
                engine_mode="event_proxy",
            )
            return item

        runs = [run("demo-a"), run("demo-b")]
        with self.assertRaises(HTTPException) as rejected:
            _validate_scored_runs(runs, allow_mixed=False)
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertEqual(rejected.exception.detail["reason"], "demo_only_event_dataset")

        _validate_scored_runs(runs, allow_mixed=True)
        protocol = _validate_protocols(runs, allow_mixed=True)
        self.assertEqual(protocol["comparison_mode"], "exploration")
        self.assertEqual(protocol["reason"], "demo_only_event_dataset_exploration")
        self.assertFalse(protocol["formal"])
        self.assertFalse(protocol["formal_evaluation_eligible"])
        self.assertEqual(protocol["demo_only_run_ids"], ["demo-a", "demo-b"])

    def test_portfolio_arena_is_unchanged(self) -> None:
        from app.routes.arena import _validate_scored_runs

        result = self.root / "result.json"
        result.write_text("{}", encoding="utf-8")
        run = {
            "id": "portfolio",
            "status": "done",
            "engine_mode": "portfolio",
            "result_nature": "simulated_from_real_bars",
            "result_path": str(result),
            "strategy_type": "quant",
            "strategy_spec": {"type": "quant", "kind": "buy_hold"},
        }
        _validate_scored_runs([run])
        self.assertNotIn("_arena_semantic_quality", run)


if __name__ == "__main__":
    unittest.main()


def test_title_and_collector_metadata_are_not_complete_event_facts(tmp_path):
    from app.event_backtest.datasets import inspect_event_semantic_quality
    title = "公司发布经审计年度报告"
    texts = [title, title + " | 600000 浦发银行 · 定期报告", "10-K | " + title,
             "600000 浦发银行 · 年度报告"]
    rows = [{**TestEventDatasetSemanticQuality._real_row(str(index)), "event_text": text}
            for index, text in enumerate(texts)]
    path = tmp_path / "seed-metadata.jsonl"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    quality = inspect_event_semantic_quality(path)
    assert quality["metadata_only_ratio"] == 1
    assert quality["fact_complete_ratio"] == 0
    assert quality["status"] == "partial"
    assert "metadata_only_event_text" in quality["reason_codes"]
    assert quality["synthetic_ratio"] == 0


def test_short_actual_fact_summaries_are_not_rejected_by_length(tmp_path):
    from app.event_backtest.datasets import inspect_event_semantic_quality
    summaries = ["净利润同比增18%。", "央行降准0.5个百分点。", "CPI | 实际2.8%，预期3.0%。",
                 "600000 浦发银行 · 归母净利润同比增长18%"]
    rows = [{**TestEventDatasetSemanticQuality._real_row(str(index)), "event_text": text}
            for index, text in enumerate(summaries)]
    path = tmp_path / "short-facts.jsonl"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    quality = inspect_event_semantic_quality(path)
    assert quality["metadata_only_ratio"] == 0
    assert quality["fact_complete_ratio"] == 1
    assert quality["status"] == "research_ready"


def test_old_semantic_scan_is_refreshed_without_mutating_frozen_text(tmp_path):
    from app.event_backtest.datasets import with_semantic_quality
    row = TestEventDatasetSemanticQuality._real_row()
    row["event_text"] = row["title"]
    path = tmp_path / "old-seed.jsonl"
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n")
    before = path.read_bytes()
    old_scan = {"scan_version": "event-semantic-quality-v1", "status": "research_ready", "fact_complete_ratio": 1}
    contract = {"path": str(path), "dataset_kind": "event", "semantic_quality": old_scan}
    refreshed = with_semantic_quality(contract)
    assert refreshed["semantic_quality"]["status"] == "partial"
    assert contract["semantic_quality"] == old_scan
    assert path.read_bytes() == before
