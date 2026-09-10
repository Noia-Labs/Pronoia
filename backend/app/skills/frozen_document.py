"""Fetch one frozen announcement document for strict as-of event research.

Only allowlisted first-party disclosure hosts are supported.  This is not a
general web browser: the caller must provide the source URL/key already frozen
in the Event Packet.
"""
from __future__ import annotations

import asyncio
from datetime import date
import hashlib
import re
import time
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import requests

from .registry import err, meta, ok, skill


_EASTMONEY_HOSTS = {"data.eastmoney.com", "np-cnotice-stock.eastmoney.com"}
_SEC_HOSTS = {"www.sec.gov", "sec.gov", "data.sec.gov"}
_NOTICE_CODE = re.compile(r"^AN\d+$", re.I)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _clip_content(text: str, max_chars: int) -> tuple[str, bool]:
    cleaned = re.sub(r"[ \t]+", " ", text or "").strip()
    return cleaned[:max_chars], len(cleaned) > max_chars


def _identity_ok(content: str, symbol: str, issuer_name: str) -> bool:
    compact = re.sub(r"\s+", "", content or "").lower()
    symbol_ok = bool(symbol and re.sub(r"\W", "", symbol).lower() in compact)
    issuer = re.sub(r"\s+", "", issuer_name or "").lower()
    issuer_ok = bool(len(issuer) >= 3 and issuer in compact)
    return symbol_ok or issuer_ok


def _as_of_ok(source_published_at: str | None, event_date: str) -> bool:
    if not source_published_at or not event_date:
        return True
    try:
        return date.fromisoformat(str(source_published_at)[:10]) <= date.fromisoformat(event_date[:10])
    except ValueError:
        return True


def _fetch_eastmoney(
    notice_code: str, *, symbol: str, issuer_name: str, event_date: str,
    max_chars: int, connect_timeout: float, read_timeout: float,
) -> dict[str, Any]:
    if not _NOTICE_CODE.fullmatch(notice_code or ""):
        return err("invalid_or_missing_eastmoney_notice_code")
    endpoint = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
    pages: list[str] = []
    first: dict[str, Any] = {}
    last_error = ""
    for page_index in range(1, 51):
        payload: dict[str, Any] | None = None
        for attempt in range(3):
            try:
                response = requests.get(
                    endpoint,
                    params={"art_code": notice_code.upper(), "client_source": "web", "page_index": page_index},
                    headers=headers,
                    timeout=(connect_timeout, read_timeout),
                )
                response.raise_for_status()
                payload = response.json().get("data") or {}
                break
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))
        if payload is None:
            return err(f"eastmoney_fetch_failed: {last_error}")
        if page_index == 1:
            first = payload
        page = str(payload.get("notice_content") or "").strip()
        if page:
            pages.append(page)
        page_count = int(payload.get("page_size") or 1)
        if page_index >= page_count:
            break
    full_content = "\n\n".join(pages).strip()
    content, truncated = _clip_content(full_content, max_chars)
    published = first.get("eitime") or first.get("notice_date")
    identity = _identity_ok(full_content, symbol, issuer_name)
    as_of = _as_of_ok(str(published or ""), event_date)
    if len(full_content) < 160:
        return err("eastmoney_document_empty_or_too_short")
    if not identity:
        return err("announcement_identity_mismatch")
    if not as_of:
        return err("announcement_published_after_event_date")
    return ok({
        "content": content,
        "content_chars": len(full_content),
        "content_truncated": truncated,
        "content_sha256": hashlib.sha256(full_content.encode("utf-8")).hexdigest(),
        "source_published_at": published,
        "notice_title": first.get("notice_title"),
        "attachment_url": first.get("attach_url_web") or first.get("attach_url"),
        "pages": len(pages),
        "identity_ok": identity,
        "as_of_ok": as_of,
        "source_kind": "eastmoney_frozen_announcement",
    }, meta("frozen_announcement_fetch", 1, endpoint))


def _fetch_sec(
    source_url: str, *, symbol: str, issuer_name: str, event_date: str,
    max_chars: int, connect_timeout: float, read_timeout: float,
) -> dict[str, Any]:
    if _host(source_url) not in _SEC_HOSTS:
        return err("source_host_not_allowlisted")
    headers = {"User-Agent": "Pronoia event research research@example.com"}
    last_error = ""
    for attempt in range(3):
        try:
            response = requests.get(
                source_url, headers=headers, timeout=(connect_timeout, read_timeout),
            )
            response.raise_for_status()
            if _host(str(response.url or source_url)) not in _SEC_HOSTS:
                return err("redirect_host_not_allowlisted")
            content_type = str(response.headers.get("content-type") or "").lower()
            if "html" in content_type or source_url.lower().endswith((".htm", ".html")):
                full_content = BeautifulSoup(response.content, "lxml").get_text(" ", strip=True)
            else:
                full_content = response.text.strip()
            break
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
    else:
        return err(f"sec_fetch_failed: {last_error}")
    content, truncated = _clip_content(full_content, max_chars)
    identity = _identity_ok(full_content, symbol, issuer_name)
    if len(full_content) < 160:
        return err("sec_document_empty_or_too_short")
    if not identity:
        return err("announcement_identity_mismatch")
    return ok({
        "content": content,
        "content_chars": len(full_content),
        "content_truncated": truncated,
        "content_sha256": hashlib.sha256(full_content.encode("utf-8")).hexdigest(),
        "source_published_at": event_date,
        "identity_ok": identity,
        "as_of_ok": True,
        "source_kind": "sec_frozen_filing",
    }, meta("frozen_announcement_fetch", 1, source_url))


@skill(
    "frozen_announcement_fetch",
    "读取 Event Packet 中已冻结的公告来源正文。只允许东财公告和 SEC 官方域名，"
    "并校验证券身份与 as-of 日期；不是开放网页搜索。元数据 packet 必须先调用本技能再解读事件。",
    {
        "type": "object",
        "properties": {
            "source_url": {"type": "string", "description": "Event Packet 中冻结的来源 URL"},
            "source_key": {"type": "string", "description": "东财 AN 公告代码或 SEC accession"},
            "market": {"type": "string", "enum": ["CN", "US"]},
            "symbol": {"type": "string"},
            "issuer_name": {"type": "string"},
            "event_date": {"type": "string", "description": "YYYY-MM-DD，严格 as-of 上限"},
            "max_chars": {"type": "integer", "minimum": 1000, "maximum": 30000},
        },
        "required": [],
        "additionalProperties": False,
    },
    category="skill",
    internal=False,
)
async def frozen_announcement_fetch(
    source_url: str = "", source_key: str = "", market: str = "CN",
    symbol: str = "", issuer_name: str = "", event_date: str = "",
    max_chars: int = 12000,
) -> dict[str, Any]:
    market = (market or "").upper()
    host = _host(source_url)
    if market == "CN":
        if host and host not in _EASTMONEY_HOSTS:
            return err("source_host_not_allowlisted")
        notice_code = source_key or ""
        if not notice_code:
            match = re.search(r"/(AN\d+)\.html", source_url or "", re.I)
            notice_code = match.group(1) if match else ""
        return await asyncio.to_thread(
            _fetch_eastmoney, notice_code, symbol=symbol, issuer_name=issuer_name,
            event_date=event_date, max_chars=max_chars, connect_timeout=6.0, read_timeout=25.0,
        )
    if market == "US":
        return await asyncio.to_thread(
            _fetch_sec, source_url, symbol=symbol, issuer_name=issuer_name,
            event_date=event_date, max_chars=max_chars, connect_timeout=6.0, read_timeout=30.0,
        )
    return err("unsupported_market_for_frozen_document")
