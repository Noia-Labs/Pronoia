"""Security regression tests for env-backed external integration secrets."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class TestExternalSecretPolicy(unittest.TestCase):
    RESERVED_PROCESS_ENV = (
        "ARK_API_KEY",
        "MAAS_API_KEY",
        "PRONOIA_SHARE_PASSWORD",
        "PATH",
    )

    def test_strategy_reference_allowlist_rejects_process_secrets(self):
        from app.model_endpoint_security import validate_secret_env_reference

        self.assertEqual(
            validate_secret_env_reference(
                "env:PRONOIA_STRATEGY_SECRET_VENDOR_1",
                namespace="strategy",
                label="test",
            ),
            "PRONOIA_STRATEGY_SECRET_VENDOR_1",
        )
        for name in (*self.RESERVED_PROCESS_ENV, "PRONOIA_DATA_SECRET_VENDOR", "PRONOIA_STRATEGY_SECRET_"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "PRONOIA_STRATEGY_SECRET_"):
                validate_secret_env_reference(
                    f"env:{name}",
                    namespace="strategy",
                    label="test",
                )

    def test_data_source_uses_separate_namespace(self):
        from app.schemas import DatasetSourceSpec

        accepted = DatasetSourceSpec.model_validate({
            "type": "api",
            "provider": "vendor",
            "ref": "dataset://daily",
            "secret_env_ref": "PRONOIA_DATA_SECRET_VENDOR_1",
        })
        self.assertEqual(accepted.secret_env_ref, "PRONOIA_DATA_SECRET_VENDOR_1")
        for name in (*self.RESERVED_PROCESS_ENV, "PRONOIA_STRATEGY_SECRET_VENDOR", "PRONOIA_DATA_SECRET_"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "PRONOIA_DATA_SECRET_"):
                DatasetSourceSpec.model_validate({
                    "type": "api",
                    "secret_env_ref": name,
                })

    def test_strategy_creation_rejects_reserved_and_cross_namespace_refs(self):
        from app.event_backtest.strategy_registry import normalize_strategy_spec

        for family in ("event", "api"):
            for name in (*self.RESERVED_PROCESS_ENV, "PRONOIA_DATA_SECRET_VENDOR"):
                spec = {
                    "type": family,
                    "adapter": "external_http",
                    "endpoint": "https://8.8.8.8/model",
                    "headers": {"Authorization": f"env:{name}"},
                }
                with self.subTest(family=family, name=name), self.assertRaisesRegex(
                    ValueError, "PRONOIA_STRATEGY_SECRET_",
                ):
                    normalize_strategy_spec(spec, legacy_runner=None, legacy_strategy_type=None)

        normalized = normalize_strategy_spec({
            "type": "api",
            "adapter": "external_http",
            "endpoint": "https://8.8.8.8/model",
            "headers": {"Authorization": "env:PRONOIA_STRATEGY_SECRET_VENDOR"},
            "parameters": {"secret_env_ref": "PRONOIA_STRATEGY_SECRET_VENDOR"},
        }, legacy_runner=None, legacy_strategy_type=None)
        self.assertEqual(
            normalized["headers"]["Authorization"],
            "env:PRONOIA_STRATEGY_SECRET_VENDOR",
        )

    def test_event_runtime_revalidates_bypassed_spec(self):
        from app.event_backtest.external_strategy import _headers

        with patch.dict(os.environ, {
            "ARK_API_KEY": "must-not-leak",
            "PRONOIA_STRATEGY_SECRET_VENDOR": "allowed-secret",
        }, clear=False):
            with self.assertRaisesRegex(ValueError, "PRONOIA_STRATEGY_SECRET_"):
                _headers({"headers": {"Authorization": "env:ARK_API_KEY"}})
            resolved = _headers({
                "headers": {"Authorization": "env:PRONOIA_STRATEGY_SECRET_VENDOR"},
            })
        self.assertEqual(resolved["Authorization"], "allowed-secret")

    def test_quant_constructor_and_runtime_reject_bypass(self):
        from app.quant_backtest.models import Bar, MarketDataset, QuantBacktestError
        from app.quant_backtest.strategies import ExternalHTTPStrategy

        with self.assertRaisesRegex(QuantBacktestError, "PRONOIA_STRATEGY_SECRET_"):
            ExternalHTTPStrategy(
                "https://model.example/signals",
                headers={"Authorization": "env:MAAS_API_KEY"},
            )

        strategy = ExternalHTTPStrategy(
            "https://model.example/signals",
            headers={"Authorization": "env:PRONOIA_STRATEGY_SECRET_VENDOR"},
        )
        # Simulate a persisted/legacy object or in-process mutation bypassing
        # constructor validation. Execution must still fail before HTTP opens.
        strategy.headers["Authorization"] = "env:PRONOIA_SHARE_PASSWORD"
        dataset = MarketDataset(
            name="secret guard",
            symbol="TEST",
            market="US",
            frequency="1d",
            bars=(
                Bar("2025-01-02", 10, 11, 9, 10),
                Bar("2025-01-03", 10, 12, 9, 11),
            ),
        )
        with patch.dict(os.environ, {"PRONOIA_SHARE_PASSWORD": "must-not-leak"}, clear=False), patch(
            "app.quant_backtest.strategies.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        ), patch("app.quant_backtest.strategies.build_opener") as opener:
            with self.assertRaisesRegex(QuantBacktestError, "PRONOIA_STRATEGY_SECRET_"):
                strategy.generate(dataset)
            opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
