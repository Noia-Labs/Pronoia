"""Portable seed paths and public-sharing path boundary regressions."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


class TestPortableQuantSeedPath(unittest.TestCase):
    def test_default_is_project_seed_data_and_env_override_is_explicit(self) -> None:
        from app.config import PROJECT_ROOT
        from app.quant_backtest.service import _default_futures_daily_dir

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRONOIA_FUTURES_DAILY_DIR", None)
            default = _default_futures_daily_dir()
        self.assertEqual(default, PROJECT_ROOT / "seed_data" / "futures" / "daily")
        self.assertNotIn("指数测算_v1.3", str(default))

        with patch.dict(os.environ, {"PRONOIA_FUTURES_DAILY_DIR": "/srv/pronoia/futures"}):
            self.assertEqual(_default_futures_daily_dir(), Path("/srv/pronoia/futures"))


class TestBacktestSharePathBoundary(unittest.TestCase):
    def test_local_mode_preserves_existing_absolute_path_behavior(self) -> None:
        from app.routes import backtest

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"PRONOIA_SHARE_MODE": ""}, clear=False,
        ):
            candidate = Path(temp_dir) / "bars.csv"
            self.assertEqual(backtest._resolve_path(str(candidate)), str(candidate))

    def test_share_mode_accepts_only_seed_or_managed_data_roots(self) -> None:
        from app.routes import backtest

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            managed = temp_root / "managed"
            outside = temp_root / "private" / "secret.csv"
            with patch.object(backtest, "DATA_DIR", str(managed)), patch.dict(
                os.environ, {"PRONOIA_SHARE_MODE": "true"}, clear=False,
            ):
                project_file = backtest.PROJECT_ROOT / "seed_data" / "sample.csv"
                managed_file = managed / "market" / "sample.csv"
                self.assertEqual(backtest._resolve_path(str(project_file)), str(project_file.resolve()))
                self.assertEqual(backtest._resolve_path(str(managed_file)), str(managed_file.resolve()))

                for untrusted in (
                    str(outside),
                    "../outside-project.csv",
                    str(backtest.PROJECT_ROOT / ".env"),
                    str(backtest.PROJECT_ROOT / "backend" / "app" / "config.py"),
                ):
                    with self.subTest(path=untrusted), self.assertRaises(HTTPException) as raised:
                        backtest._resolve_path(untrusted)
                    self.assertEqual(raised.exception.status_code, 403)
                    self.assertNotIn(str(outside), str(raised.exception.detail))

    def test_share_dtos_replace_absolute_paths_but_keep_provider_urls(self) -> None:
        from app.routes import backtest

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            managed = temp_root / "managed"
            outside = temp_root / "private" / "secret.jsonl"
            project_file = backtest.PROJECT_ROOT / "seed_data" / "events.jsonl"
            with patch.object(backtest, "DATA_DIR", str(managed)), patch.dict(
                os.environ, {"PRONOIA_SHARE_MODE": "1"}, clear=False,
            ):
                payload = backtest._share_safe_payload({
                    "events_path": str(project_file),
                    "result_path": str(managed / "results" / "result.json"),
                    "source": {"ref": str(outside), "docs": "https://example.com/path"},
                    "error_msg": f"cannot read {outside}",
                })

            self.assertEqual(payload["events_path"], "project://seed_data/events.jsonl")
            self.assertEqual(payload["result_path"], "data://results/result.json")
            self.assertEqual(payload["source"]["ref"], "[redacted-local-path]")
            self.assertEqual(payload["source"]["docs"], "https://example.com/path")
            self.assertNotIn(str(outside), payload["error_msg"])
            self.assertIn("[redacted-local-path]", payload["error_msg"])

    def test_dataset_response_does_not_publish_server_paths_in_share_mode(self) -> None:
        from app.routes import backtest

        with tempfile.TemporaryDirectory() as temp_dir:
            managed = Path(temp_dir) / "managed"
            source_path = managed / "bars.csv"
            dataset = {
                "id": "share-bars",
                "name": "Share bars",
                "dataset_kind": "market",
                "path": str(source_path),
                "labels_path": None,
                "source": {"type": "local_file", "ref": str(source_path)},
                "status": "available",
                "markets": ["CN"],
                "symbols": ["000300.SH"],
            }
            with patch.object(backtest, "DATA_DIR", str(managed)), patch.dict(
                os.environ, {"PRONOIA_SHARE_MODE": "yes"}, clear=False,
            ):
                response = backtest._dataset_response(dataset).model_dump()

            self.assertEqual(response["path"], "data://bars.csv")
            self.assertEqual(response["source"]["ref"], "data://bars.csv")
            self.assertNotIn(str(managed), str(response))


if __name__ == "__main__":
    unittest.main()
