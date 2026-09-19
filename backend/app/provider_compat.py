"""Narrow transport defaults for official DeepSeek's dual-mode API."""
import os
from urllib.parse import urlsplit


def direct_deepseek(base_url: str) -> bool:
    if urlsplit(str(base_url)).hostname != "api.deepseek.com":
        return False
    mode = (os.environ.get("DEEPSEEK_TRANSPORT") or "system").strip().lower()
    if mode not in {"system", "direct"}:
        raise ValueError("DEEPSEEK_TRANSPORT must be system or direct")
    return mode == "direct"


def chat_request_options(base_url: str, thinking_mode: str = "auto") -> dict:
    # Auto uses content-only completions for bounded tool/JSON tasks. Explicit
    # Model Lab thinking settings remain available for controlled comparisons.
    if urlsplit(str(base_url)).hostname != "api.deepseek.com":
        return {}
    mode = "enabled" if thinking_mode == "enabled" else "disabled"
    return {"extra_body": {"thinking": {"type": mode}}}
