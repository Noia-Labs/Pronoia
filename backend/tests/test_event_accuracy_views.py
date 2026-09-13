from __future__ import annotations

import unittest

from app.event_backtest.arena import ArenaRunContext, _forecast_paired_test
from app.event_backtest.metrics_registry import compute_all_metrics
from app.event_backtest.models import EventLabel, TeamPrediction


def _label(event_id: str, direction: str) -> EventLabel:
    car = {"up": 0.02, "down": -0.02, "neutral": 0.0}[direction]
    return EventLabel(
        event_id=event_id,
        label_t1=direction,
        label_t3=direction,
        label_t5=direction,
        car_t1=car,
        car_t3=car,
        car_t5=car,
    )


class EventAccuracyViewsTests(unittest.TestCase):
    def test_directional_and_three_class_are_separate(self) -> None:
        predictions = [
            TeamPrediction(event_id="up-hit", pred_direction="up", run_id="r"),
            TeamPrediction(event_id="down-miss", pred_direction="up", run_id="r"),
            TeamPrediction(event_id="neutral-hit", pred_direction="neutral", run_id="r"),
            TeamPrediction(event_id="neutral-no-trade", pred_direction="neutral", run_id="r"),
            TeamPrediction(event_id="invalid", pred_direction="neutral", run_id="r", abstain=True),
        ]
        labels = [
            _label("up-hit", "up"),
            _label("down-miss", "down"),
            _label("neutral-hit", "neutral"),
            _label("neutral-no-trade", "up"),
            _label("invalid", "neutral"),
        ]

        metrics = compute_all_metrics(predictions=predictions, labels=labels)

        directional = metrics["acc_primary_directional_trade"]
        self.assertEqual(directional.value, 0.5)
        self.assertEqual(directional.meta["n"], 2)
        self.assertEqual(directional.meta["k"], 1)

        three_class = metrics["acc_primary_three_class"]
        self.assertEqual(three_class.value, 0.5)
        self.assertEqual(three_class.meta["n"], 4)
        self.assertEqual(three_class.meta["k"], 2)

    def test_no_directional_trade_is_missing_not_zero(self) -> None:
        metrics = compute_all_metrics(
            predictions=[TeamPrediction(event_id="neutral", pred_direction="neutral", run_id="r")],
            labels=[_label("neutral", "neutral")],
        )
        self.assertIsNone(metrics["acc_primary_directional_trade"].value)
        self.assertEqual(metrics["acc_primary_three_class"].value, 1.0)

    def test_arena_three_class_pairwise_counts_neutral_match(self) -> None:
        left = ArenaRunContext(
            run_id="left",
            run_info={},
            metrics={"acc_primary_three_class": {"meta": {"primary_horizon": "t3"}}},
            predictions_by_eid={"e": {"pred_direction": "neutral", "abstain": False}},
        )
        right = ArenaRunContext(
            run_id="right",
            run_info={},
            metrics={"acc_primary_three_class": {"meta": {"primary_horizon": "t3"}}},
            predictions_by_eid={"e": {"pred_direction": "up", "abstain": False}},
        )

        result = _forecast_paired_test(
            left,
            right,
            "acc_primary_three_class",
            {"e": {"label_t3": "neutral"}},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["n_paired"], 1)
        self.assertEqual(result["b_only_a_correct"], 1)
        self.assertEqual(result["c_only_b_correct"], 0)

    def test_arena_directional_pairwise_uses_shared_trade_population(self) -> None:
        left = ArenaRunContext(
            run_id="left",
            run_info={},
            metrics={"acc_primary_directional_trade": {"meta": {"primary_horizon": "t3"}}},
            predictions_by_eid={
                "one-sided": {"pred_direction": "up", "abstain": False},
                "shared": {"pred_direction": "down", "abstain": False},
            },
        )
        right = ArenaRunContext(
            run_id="right",
            run_info={},
            metrics={"acc_primary_directional_trade": {"meta": {"primary_horizon": "t3"}}},
            predictions_by_eid={
                "one-sided": {"pred_direction": "neutral", "abstain": False},
                "shared": {"pred_direction": "up", "abstain": False},
            },
        )

        result = _forecast_paired_test(
            left,
            right,
            "acc_primary_directional_trade",
            {
                "one-sided": {"label_t3": "up"},
                "shared": {"label_t3": "down"},
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["n_paired"], 1)
        self.assertEqual(result["b_only_a_correct"], 1)
        self.assertEqual(result["c_only_b_correct"], 0)


if __name__ == "__main__":
    unittest.main()
