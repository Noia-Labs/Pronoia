#!/usr/bin/env python3
"""Bounded, non-secret health probe for the configured LLM gateway."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Clear inherited desktop proxies before importing config or constructing the
# OpenAI-compatible client.  backend.app.llm also sets trust_env=False.
for _key in (
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy",
):
    os.environ.pop(_key, None)

from app import config  # noqa: E402
from app.llm import get_client  # noqa: E402


async def _probe(timeout: float) -> dict:
    started = time.monotonic()
    host = urlparse(config.LLM_BASE_URL).hostname or ""
    try:
        response = await asyncio.wait_for(
            get_client().chat.completions.create(
                model=config.LLM_MODEL,
                messages=[{"role": "user", "content": "Reply with exactly OK."}],
                temperature=0,
                max_tokens=8,
            ),
            timeout=timeout,
        )
        text = (response.choices[0].message.content or "").strip()
        return {
            "ok": True,
            "host": host,
            "model": config.LLM_MODEL,
            "wall_seconds": round(time.monotonic() - started, 3),
            "response_received": bool(text),
            "response_preview": text[:20],
        }
    except Exception as exc:  # noqa: BLE001
        cause = exc.__cause__ or exc.__context__
        return {
            "ok": False,
            "host": host,
            "model": config.LLM_MODEL,
            "wall_seconds": round(time.monotonic() - started, 3),
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
            "cause_type": type(cause).__name__ if cause else "",
            "cause": str(cause)[:300] if cause else "",
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    result = asyncio.run(_probe(max(1.0, args.timeout)))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
