"""FEVER backend configuration: env loading & tunables."""
from __future__ import annotations

import os
from pathlib import Path

# akshare 部分接口用 tqdm 打进度条，污染服务日志，全局禁用
os.environ.setdefault("TQDM_DISABLE", "1")

from dotenv import load_dotenv

# backend/app/config.py -> app -> backend -> Pronoia project root
_BACKEND_DIR = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _BACKEND_DIR.parent

# Project-root .env first (shared, contains the real ARK_API_KEY),
# then backend-local .env may override.
load_dotenv(_PROJECT_ROOT / ".env", override=False)
load_dotenv(_BACKEND_DIR / ".env", override=True)

ARK_API_URL: str = os.getenv("ARK_API_URL", "")
ARK_API_KEY: str = os.getenv("ARK_API_KEY", "")
ARK_MODEL: str = os.getenv("ARK_MODEL", "deepseek-v4-flash")

MAAS_API_URL: str = os.getenv("MAAS_API_URL", "")
MAAS_API_KEY: str = os.getenv("MAAS_API_KEY", "")
MAAS_MODEL: str = os.getenv("MAAS_MODEL", "")

def resolve_llm() -> tuple[str, str, str]:
    """返回 (base_url, api_key, model)。优先级：MAAS → ARK；自动修正 URL/KEY 写反的容错。"""
    maas_url = str(MAAS_API_URL or "").strip()
    maas_key = str(MAAS_API_KEY or "").strip()
    maas_model = str(MAAS_MODEL or "").strip()
    if maas_url and maas_key:
        u_is_url = maas_url.startswith("http://") or maas_url.startswith("https://")
        k_is_url = maas_key.startswith("http://") or maas_key.startswith("https://")
        if (not u_is_url) and k_is_url:
            maas_url, maas_key = maas_key, maas_url
            u_is_url, k_is_url = True, False
        if u_is_url and (not k_is_url) and maas_model:
            return (maas_url.rstrip("/"), maas_key, maas_model)
    # fallback ARK
    return (str(ARK_API_URL or "").rstrip("/"), str(ARK_API_KEY or ""), str(ARK_MODEL or "deepseek-v4-flash"))

LLM_BASE_URL, LLM_API_KEY, LLM_MODEL = resolve_llm()

DB_PATH: str = os.getenv("FEVER_DB_PATH", str(_BACKEND_DIR / "fever.db"))
DATA_DIR: str = os.getenv("FEVER_DATA_DIR", str(_PROJECT_ROOT / "data"))

# Long-running actor simulation is isolated behind an asynchronous gateway.
SIMULATION_GATEWAY_URL: str = os.getenv(
    "FEVER_SIMULATION_GATEWAY_URL", "http://127.0.0.1:5010"
).rstrip("/")
SIMULATION_GATEWAY_TIMEOUT: float = float(
    os.getenv("FEVER_SIMULATION_GATEWAY_TIMEOUT", "15")
)

# Optional whole-site HTTP Basic protection for a deliberately shared instance.
# Keeping both values empty preserves the local-first, login-free experience.
SHARE_USER: str = os.getenv("PRONOIA_SHARE_USER", "")
SHARE_PASSWORD: str = os.getenv("PRONOIA_SHARE_PASSWORD", "")


def validate_share_auth_config() -> None:
    """Reject an incomplete sharing configuration without exposing secrets."""
    if bool(SHARE_USER) != bool(SHARE_PASSWORD):
        raise RuntimeError(
            "PRONOIA_SHARE_USER and PRONOIA_SHARE_PASSWORD must be configured together"
        )
    if SHARE_USER and ":" in SHARE_USER:
        raise RuntimeError("PRONOIA_SHARE_USER must not contain a colon")
    if SHARE_PASSWORD and len(SHARE_PASSWORD) < 12:
        raise RuntimeError("PRONOIA_SHARE_PASSWORD must contain at least 12 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in SHARE_USER + SHARE_PASSWORD):
        raise RuntimeError("Pronoia sharing credentials must not contain control characters")


def share_auth_enabled() -> bool:
    """Return whether whole-site sharing protection is enabled."""
    return bool(SHARE_USER and SHARE_PASSWORD)

# Local-first security boundary. Additional trusted web origins can be supplied
# explicitly for a deployed instance; the default never exposes data to arbitrary
# sites through permissive CORS.
CORS_ORIGINS: list[str] = [
    item.strip()
    for item in os.getenv(
        "PRONOIA_CORS_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173",
    ).split(",")
    if item.strip()
]

# Skill execution guardrails (design.md §2/§4)
#
# Trajectory analysis showed that every top-level 60s timeout added roughly
# 63~68s to a sample.  A single deadline also caused a race between composite
# skills and their children: the parent expired just as a child was returning
# its timeout result, so already-completed sibling results were lost.  Keep the
# public/root budget at 60s, but make nested budgets shorter so composites have
# time to aggregate partial results.
SKILL_TIMEOUT: float = float(os.getenv("FEVER_SKILL_TIMEOUT", "60"))
SKILL_SUB_TIMEOUT: float = float(os.getenv("FEVER_SKILL_SUB_TIMEOUT", "30"))
SKILL_SLOW_SUB_TIMEOUT: float = float(os.getenv("FEVER_SKILL_SLOW_SUB_TIMEOUT", "45"))
SKILL_COMPOSITE_SUB_TIMEOUT: float = float(os.getenv("FEVER_SKILL_COMPOSITE_SUB_TIMEOUT", "50"))
TOOL_RESULT_MAX_CHARS: int = int(os.getenv("FEVER_TOOL_RESULT_MAX_CHARS", "4000"))

# Agent loop guardrails (design.md §6)
AUTO_MAX_ROUNDS: int = int(os.getenv("FEVER_AUTO_MAX_ROUNDS", "8"))
TEAM_MAX_ROUNDS: int = int(os.getenv("FEVER_TEAM_MAX_ROUNDS", "5"))
CONTEXT_MESSAGES: int = int(os.getenv("FEVER_CONTEXT_MESSAGES", "12"))

# Evidence Navigator runs after the initial evidence graph is assembled.  The
# defaults deliberately permit only one focused verification pass so a weak or
# sparse graph cannot turn into an unbounded research loop.
EVIDENCE_NAVIGATOR_ENABLED: bool = os.getenv("FEVER_EVIDENCE_NAVIGATOR", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
EVIDENCE_NAVIGATOR_MAX_ROUNDS: int = max(0, int(os.getenv("FEVER_EVIDENCE_NAVIGATOR_MAX_ROUNDS", "1")))
EVIDENCE_NAVIGATOR_MAX_DISPATCHES: int = max(1, int(os.getenv("FEVER_EVIDENCE_NAVIGATOR_MAX_DISPATCHES", "1")))
EVIDENCE_NAVIGATOR_FOLLOWUP_MAX_ROUNDS: int = max(
    1, int(os.getenv("FEVER_EVIDENCE_NAVIGATOR_FOLLOWUP_MAX_ROUNDS", "3"))
)
EVIDENCE_NAVIGATOR_EXTERNAL_SKILL_BUDGET: int = max(
    1, int(os.getenv("FEVER_EVIDENCE_NAVIGATOR_EXTERNAL_SKILL_BUDGET", "1"))
)

# LLM request timeout (per streaming round)
LLM_TIMEOUT: float = float(os.getenv("FEVER_LLM_TIMEOUT", "180"))
LLM_FORCE_IPV4: bool = os.getenv("FEVER_LLM_FORCE_IPV4", "").strip().lower() in {
    "1", "true", "yes", "on",
}

# 生成上限（回测/采集提速：不设则模型默认 max_tokens，生成长、拖慢每事件耗时）
AGENT_MAX_TOKENS: int = int(os.getenv("FEVER_AGENT_MAX_TOKENS", "0"))

FRONTEND_DIST = _PROJECT_ROOT / "frontend" / "dist"
PROJECT_ROOT: Path = _PROJECT_ROOT
BACKEND_DIR: Path = _BACKEND_DIR
