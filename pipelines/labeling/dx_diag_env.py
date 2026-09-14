"""诊断 config 是否能正确加载 LLM 密钥"""
import sys, os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _PROJECT_ROOT / "backend"
sys.path.insert(0, str(_BACKEND_DIR))

pr = (_BACKEND_DIR / "app" / "config.py").resolve().parents[1]
print("config PROJECT_ROOT parent =", pr)   # -> backend
print("parent.parent (project root) =", pr.parent)
env_file = pr.parent / '.env'
print("project .env exists:", env_file.exists())

# 直接读 .env
if env_file.exists():
    for line in open(env_file):
        line=line.strip()
        if line.startswith('LLM_API') or line.startswith('MAAS_') or line.startswith('OPENAI'):
            k,v=line.split('=',1) if '=' in line else (line,'')
            print(f"  {k} = {v[:8]}... len={len(v)}")

from app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, PROJECT_ROOT
print("\nPROJECT_ROOT (from config):", PROJECT_ROOT)
print("LLM_BASE_URL[:60]:", (LLM_BASE_URL or '')[:60])
print("LLM_API_KEY :", ('HAS len='+str(len(LLM_API_KEY))) if LLM_API_KEY else 'EMPTY!!!')
print("LLM_MODEL   :", LLM_MODEL)
