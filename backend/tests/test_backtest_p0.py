"""P0 回测后端单元测试：DB CRUD → 写入 predictions/labels → compute_metrics → REST API。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


class TestBacktestP0(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 隔离测试环境：临时 DB + DATA_DIR
        cls._td = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._td.name)
        cls._old_env_db = os.environ.get("FEVER_DB_PATH")
        cls._old_env_data = os.environ.get("FEVER_DATA_DIR")
        os.environ["FEVER_DB_PATH"] = str(cls.tmp / "fever_test.db")
        os.environ["FEVER_DATA_DIR"] = str(cls.tmp / "data")
        (cls.tmp / "data").mkdir(exist_ok=True)
        # pytest 会在 setUpClass 之前收集/导入其他模块，所以仅修改环境
        # 变量不足以改变已加载的 config.DB_PATH。显式替换运行时配置，
        # 避免单独或全量运行测试时污染用户的 fever.db。
        import app.config as _config
        import app.db as _db
        from app.routes import backtest as _backtest

        cls._old_config_db = _config.DB_PATH
        cls._old_config_data = _config.DATA_DIR
        cls._old_route_data = _backtest.DATA_DIR
        if _db._conn is not None:
            _db._conn.close()
        _db._conn = None
        _config.DB_PATH = os.environ["FEVER_DB_PATH"]
        _config.DATA_DIR = os.environ["FEVER_DATA_DIR"]
        _backtest.DATA_DIR = os.environ["FEVER_DATA_DIR"]
        _db.init_db()
        cls.events_path = cls._make_fixture_events(cls.tmp / "events.jsonl")
        cls.labels_path = cls._make_fixture_labels(cls.tmp / "labels.jsonl")
        cls.predictions_path = cls.tmp / "predictions.jsonl"

    @classmethod
    def tearDownClass(cls):
        import app.config as _config
        import app.db as _db
        from app.routes import backtest as _backtest

        if _db._conn is not None:
            _db._conn.close()
        _db._conn = None
        _config.DB_PATH = cls._old_config_db
        _config.DATA_DIR = cls._old_config_data
        _backtest.DATA_DIR = cls._old_route_data
        if cls._old_env_db is None:
            os.environ.pop("FEVER_DB_PATH", None)
        else:
            os.environ["FEVER_DB_PATH"] = cls._old_env_db
        if cls._old_env_data is None:
            os.environ.pop("FEVER_DATA_DIR", None)
        else:
            os.environ["FEVER_DATA_DIR"] = cls._old_env_data
        cls._td.cleanup()

    @staticmethod
    def _make_fixture_events(p: Path) -> Path:
        # 12 条事件：8 CN / 4 US，覆盖 4 种 event_type_l2
        evs = []
        markets = ["CN"] * 8 + ["US"] * 4
        types = (["并购/分拆"] * 3 + ["财报"] * 2 + ["增减持"] * 2 + ["分红"] * 1
                 + ["财报"] * 2 + ["央行政策"] * 2)
        for i, (m, t) in enumerate(zip(markets, types)):
            evs.append({
                "event_id": f"e{i+1:03d}",
                "symbol": {"CN": "sh600519", "US": "NVDA"}[m] if i % 2 == 0 else {"CN": "sz000001", "US": "AAPL"}[m],
                "market": m,
                "event_time": f"2025-0{i%3+1}-{10+i:02d}T09:30:00+08:00",
                "event_type_l2": t,
                "title": f"test event {i+1}",
                "event_text": f"dummy event text for fixture {i+1}",
                "source_url": f"https://example.com/e{i+1:03d}",
            })
        with open(p, "w", encoding="utf-8") as f:
            for e in evs:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return p

    @staticmethod
    def _make_fixture_labels(p: Path) -> Path:
        # Oracle labels: 12 事件: 7 up / 3 down / 2 neutral（car_t3 < 50bps）
        dirs = ["up", "up", "down", "neutral", "up", "down", "up", "neutral", "up", "up", "down", "up"]
        cars = [0.023, 0.015, -0.031, 0.002, 0.018, -0.044, 0.021, 0.0035, 0.027, 0.011, -0.019, 0.036]
        labels = []
        for i, (d, c) in enumerate(zip(dirs, cars)):
            labels.append({
                "event_id": f"e{i+1:03d}",
                "car_t1": 0.0,
                "car_t3": c,
                "car_t5": c * 1.15,
                "label_t1": d,
                "label_t3": d,
                "label_t5": d,
            })
        with open(p, "w", encoding="utf-8") as f:
            for lbl in labels:
                f.write(json.dumps(lbl, ensure_ascii=False) + "\n")
        return p

    # ---------------------------------------------------------------- tests ---

    def test_01_create_and_list_runs(self):
        from app import db
        r = db.create_bt_run(
            name="p0 unit test",
            runner="team_full",
            events_path=str(self.events_path),
            labels_path=str(self.labels_path),
            out_path=str(self.predictions_path),
            prompt_variant="v0",
            concurrency=1,
        )
        self.assertIsNotNone(r)
        self.assertEqual(r["status"], "pending")
        self.assertEqual(r["runner"], "team_full")
        self._rid = r["id"]
        runs = db.list_bt_runs()
        self.assertTrue(any(x["id"] == r["id"] for x in runs))

    def test_02_write_predictions_and_snapshot(self):
        from app import db
        # 取 run_id = test_01 的第一个 pending run
        runs = db.list_bt_runs()
        self.assertTrue(len(runs) >= 1)
        rid = runs[0]["id"]
        db.update_bt_run_status(rid, "running")
        # 构造 12 个 TeamPrediction，故意构造 1 个误判和 2 个 neutral，其他全对
        # labels（e001..e012）labels_t3 顺序 = up up down neutral up down up neutral up up down up
        preds = [
            ("e001", "up",      0.80),   # ✓
            ("e002", "up",      0.71),   # ✓
            ("e003", "down",    0.68),   # ✓
            ("e004", "neutral", 0.50),   # neutral
            ("e005", "down",    0.60),   # ✗ (oracle up → 故意错)
            ("e006", "down",    0.70),   # ✓
            ("e007", "up",      0.65),   # ✓
            ("e008", "neutral", 0.49),   # neutral
            ("e009", "up",      0.78),   # ✓
            ("e010", "up",      0.83),   # ✓
            ("e011", "down",    0.66),   # ✓
            ("e012", "up",      0.85),   # ✓
        ]
        # 写 bt_predictions + 同步写 JSONL 让 /metrics API 可读
        from app.event_backtest.models import TeamPrediction
        from app.event_backtest.application import write_jsonl
        p_list = []
        for idx, (eid, d, c) in enumerate(preds):
            db.add_bt_prediction(
                run_id=rid,
                event_id=eid,
                pred_direction=d,
                confidence=c,
                symbol={"e001": "sh600519", "e002": "sz000001", "e003": "sh600519",
                        "e004": "sz000001", "e005": "sh600519", "e006": "sz000001",
                        "e007": "sh600519", "e008": "sz000001", "e009": "sh600519",
                        "e010": "sz000001", "e011": "NVDA", "e012": "AAPL"}[eid],
                market="CN" if idx < 8 else "US",
            event_type_l2=["并购/分拆", "并购/分拆", "并购/分拆", "财报", "财报", "财报", "增减持", "增减持",
                           "分红", "财报", "财报", "央行政策"][idx],
            )
            p_list.append(TeamPrediction(
                event_id=eid, pred_direction=d, confidence=c,
                rationale=f"test pred {eid}", run_id=rid,
            ))
        write_jsonl(self.predictions_path, [p.to_dict() for p in p_list])
        # 写 1 个 metrics 快照
        snap_id = db.add_bt_metrics_snapshot(
            run_id=rid, done_count=12,
            acc_t3_strict=0.80, acc_t3_strict_lo=0.55,
            acc_t3_non_neutral=0.90, neutral_ratio=2/12,
        )
        self.assertGreater(snap_id, 0)
        db.update_bt_run_progress(
            rid, done_events=12,
            acc_t3_strict=0.80, acc_t3_strict_lo=0.55, acc_t3_non_neutral=0.90,
        )
        db.update_bt_run_status(rid, "done")

    def test_03_compute_metrics_via_api(self):
        """通过 routes REST compute_metrics 返回 Strict ACC（含 Wilson CI）。"""
        from app import db
        from app.main import app
        from fastapi.testclient import TestClient
        c = TestClient(app)
        runs = db.list_bt_runs()
        self.assertTrue(len(runs) >= 1)
        rid = runs[0]["id"]
        r = c.get(f"/api/bt/runs/{rid}/metrics")
        self.assertEqual(r.status_code, 200, msg=r.text)
        data = r.json()
        # 12 predictions, 2 neutral: 10 non-neutral, 1 故意错 → non-neutral acc = 9/10 = 0.9
        self.assertIn("acc_t3_non_neutral", data)
        nn_acc = float(data["acc_t3_non_neutral"]["acc"])
        self.assertAlmostEqual(nn_acc, 0.9, places=3)
        # strict: 预测方向=label 且不是 neutral/oracle neutral 排除  →  9/12 或 9/10 量级
        self.assertIn("acc_t3_strict", data)
        strict_n = int(data["acc_t3_strict"]["n"])
        strict_k = int(data["acc_t3_strict"]["k"])
        strict_lo = float(data["acc_t3_strict"]["wilson_lo_95"])
        self.assertGreaterEqual(strict_n, 8)  # 至少 8 个进入分母
        self.assertEqual(strict_k / strict_n, float(data["acc_t3_strict"]["acc"]))
        self.assertIsNotNone(strict_lo)  # Wilson lo 必须有值

    def test_04_sse_broadcast_and_subscribe(self):
        from app.event_backtest import orchestrator as orch
        import asyncio

        async def _a():
            q = orch.sse_subscribe("unit_run_sse")
            orch.sse_broadcast("unit_run_sse", {"type": "prediction", "done_count": 3})
            orch.sse_broadcast("unit_run_sse", {"type": "run_done", "done_count": 12})
            first = await asyncio.wait_for(q.get(), timeout=1.0)
            second = await asyncio.wait_for(q.get(), timeout=1.0)
            return first, second

        # Python 3.11 no longer guarantees an implicit current loop, and an
        # earlier TestClient may also close it. Keep this unit test isolated.
        first, second = asyncio.run(_a())
        self.assertEqual(first["type"], "prediction")
        self.assertEqual(second["type"], "run_done")

    def test_05_rest_routes_404_and_create(self):
        """POST /api/bt/runs 创建 run 后 get 404 的场景等都 OK。"""
        from app.main import app
        from fastapi.testclient import TestClient
        c = TestClient(app)

        # 不存在的 run 应该 404
        r = c.get("/api/bt/runs/DOES_NOT_EXIST_xyz")
        self.assertEqual(r.status_code, 404)

        # 用 dataset_id 但 dataset 不存在 → 404
        r = c.post("/api/bt/runs", json={
            "name": "test via rest", "runner": "baseline",
            "dataset_id": "NOPE_NOPE",
        })
        self.assertEqual(r.status_code, 404)

        # 有效 events_path → 200 + 列表中取得到
        r = c.post("/api/bt/runs", json={
            "name": "rest created", "runner": "baseline",
            "events_path": str(self.events_path),
            "labels_path": str(self.labels_path),
            "concurrency": 1,
        })
        self.assertEqual(r.status_code, 200, msg=r.text)
        rid = r.json()["id"]
        self.assertEqual(r.json()["status"], "pending")
        self.assertEqual(r.json()["total_events"], 12)  # fixture 12 条
        r2 = c.get("/api/bt/runs")
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(any(x["id"] == rid for x in r2.json()["items"]))


if __name__ == "__main__":
    unittest.main()
