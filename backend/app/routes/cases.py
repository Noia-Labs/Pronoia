"""Cases CRUD + pin artifact + 生成研究报告 (design.md §8)."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from .. import db
from ..agents.roster import system_prompt
from ..llm import complete_text
from ..schemas import CreateCaseRequest

router = APIRouter(prefix="/api/cases", tags=["cases"])


@router.get("")
def list_cases():
    return db.list_cases()


@router.post("")
def create_case(req: CreateCaseRequest):
    return db.create_case(title=(req.title or "新研究")[:60])


@router.get("/{case_id}")
def get_case(case_id: str):
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    return {
        "case": case,
        "messages": db.list_messages(case_id),
        "artifacts": db.list_artifacts(case_id),
    }


@router.delete("/{case_id}")
def delete_case(case_id: str):
    if not db.delete_case(case_id):
        raise HTTPException(status_code=404, detail="case not found")
    return {"ok": True}


@router.post("/{case_id}/artifacts/{artifact_id}/pin")
def pin_artifact(case_id: str, artifact_id: str):
    art = db.toggle_pin(case_id, artifact_id)
    if not art:
        raise HTTPException(status_code=404, detail="artifact not found")
    return art


@router.post("/{case_id}/report")
async def generate_report(case_id: str):
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    artifacts = db.list_artifacts(case_id)
    messages = db.list_messages(case_id)

    # 产出物摘要（payload 截断，防止 prompt 过大）
    art_digest = []
    for a in artifacts[-20:]:
        payload_str = json.dumps(a["payload"], ensure_ascii=False, default=str)
        art_digest.append(
            f"- [{a['kind']}] {a['title']} (id={a['id']}, pinned={a['pinned']})\n"
            f"  payload 摘要: {payload_str[:1200]}"
        )
    dialog_digest = []
    for m in messages[-10:]:
        if m["role"] in ("user", "assistant") and (m.get("content") or "").strip():
            dialog_digest.append(f"【{m['role']}】{m['content'][:1500]}")

    prompt = (
        f"【案例标题】{case['title']}\n\n"
        f"【产出物清单】\n" + ("\n".join(art_digest) if art_digest else "（无产出物）") + "\n\n"
        f"【对话摘要】\n" + ("\n\n".join(dialog_digest) if dialog_digest else "（无对话）")
    )
    try:
        markdown = await complete_text(system_prompt("report_writer"), prompt)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"报告生成失败: {type(e).__name__}: {e}")
    if not markdown:
        raise HTTPException(status_code=502, detail="报告生成失败: 模型返回空内容")
    # 兜底：保证四段式中的免责声明小节存在（design.md §6.1 report_writer）
    if "免责声明" not in markdown:
        markdown = markdown.rstrip() + "\n\n## 免责声明\n\n仅供研究，不构成投资建议。\n"
    artifact = db.add_artifact(
        case_id, None, "report", f"{case['title']} · 研究报告", {"markdown": markdown}
    )
    return artifact
