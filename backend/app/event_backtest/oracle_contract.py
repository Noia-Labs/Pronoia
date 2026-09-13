"""Freeze the threshold of the selected Oracle at new-run creation time."""
from __future__ import annotations

import json
import math
from pathlib import Path


def _threshold(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("Oracle 收益阈值必须为 0–0.2 的数值")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Oracle 收益阈值必须为 0–0.2 的数值") from exc
    if not math.isfinite(number) or not 0 <= number <= 0.2:
        raise ValueError("Oracle 收益阈值必须为 0–0.2 的数值")
    return number


def freeze_oracle_epsilon(labels_path: str | None, requested: object = None) -> float:
    """Read the chosen label snapshot once; never infer historical run settings."""
    explicit = _threshold(requested) if requested is not None else None
    declared: set[float] = set()
    if labels_path:
        with Path(labels_path).open(encoding="utf-8-sig") as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("Oracle 标签应为 JSON 对象")
                if row.get("epsilon") is not None:
                    declared.add(_threshold(row["epsilon"]))
    if len(declared) > 1:
        raise ValueError("所选 Oracle 标签包含不同收益阈值，请先使用统一阈值生成标签")
    if declared:
        actual = next(iter(declared))
        if explicit is not None and explicit != actual:
            raise ValueError("评测收益阈值与所选 Oracle 标签不一致，请使用标签原有阈值")
        return actual
    if explicit is not None:
        return explicit
    # Older label files did not record epsilon and used the legacy 0.5% band.
    # Runs without labels use the new binary generation default.
    return 0.005 if labels_path else 0.0
