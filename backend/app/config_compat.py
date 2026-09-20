"""Compatibility for pre-Pronoia workspaces without mixing provider tuples."""
from __future__ import annotations
from collections.abc import MutableMapping, Mapping


def apply_legacy_names(environ: MutableMapping[str, str]) -> None:
    # Explicit new names always win, including deliberately empty settings.
    for name, value in list(environ.items()):
        if name.startswith("FEVER_"):
            environ.setdefault("PRONOIA_" + name[len("FEVER_"):], value)


def default_llm_settings(environ: Mapping[str, str]) -> tuple[str, str, str]:
    # Shared MiroFish .env files already use LLM_API_KEY with LLM_BASE_URL.
    # With an old ARK endpoint, carry its key and model as one provider tuple.
    if not environ.get("LLM_API_URL", "").strip() and environ.get("ARK_API_URL", "").strip():
        return (environ["ARK_API_URL"], environ.get("ARK_API_KEY", ""),
                environ.get("ARK_MODEL") or "deepseek-v4-flash")
    return (environ.get("LLM_API_URL", ""), environ.get("LLM_API_KEY", ""),
            environ.get("LLM_MODEL") or "gpt-4o-mini")
