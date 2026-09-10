#!/usr/bin/env python3
"""Collect and freeze one daily online-test event cohort.

The job writes separate raw, candidate, validated, quarantine and
prediction-eligible files.  A backfilled cohort may be useful for pipeline
testing, but it is never silently treated as a fair forward prediction set.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
import re
import sys
import time as time_module
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import akshare as ak
from bs4 import BeautifulSoup
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.skills.price_data import resolve_security_ref  # noqa: E402


SCHEMA_VERSION = "online-event-v1"
CN_TZ = ZoneInfo("Asia/Shanghai")
US_TZ = ZoneInfo("America/New_York")
CN_BENCHMARK = "sh000300"
US_BENCHMARK = "SPY"

EVENT_TYPES = (
    "并购/分拆/再融资",
    "财报超预期/不及预期",
    "公司指引上调/下调",
    "政策利率调整",
    "通胀数据意外",
    "增长/就业数据意外",
)

LOW_VALUE_CN = re.compile(
    r"投资者关系|调研|路演|会议纪要|问询函|回复|更正|补充公告|"
    r"法律意见书|专项核查|审计报告|评估报告|股东大会|董事会决议|"
    r"监事会决议|提示性公告|进展公告"
)
CN_EARNINGS = re.compile(
    r"业绩预告|业绩快报|年度报告|半年度报告|一季度报告|三季度报告|"
    r"年报|半年报|中报|季报|预增|预减|扭亏|续盈|续亏|首亏|增亏"
)
CN_MA = re.compile(
    r"要约收购|重大资产重组|重组报告书|收购报告书|发行股份购买资产|"
    r"吸收合并|分拆|定增预案|非公开发行|配股|可转债|再融资"
)
CN_GUIDANCE = re.compile(
    r"盈利预测|业绩指引|业绩展望|经营目标|年度目标|"
    r"上调.*(?:预期|预测|指引)|下调.*(?:预期|预测|指引)"
)

SEC_FORMS = {
    "8-K", "10-Q", "10-K", "10-Q/A", "10-K/A", "20-F", "40-F",
    "425", "S-4", "S-4/A", "DEFM14A", "PREM14A", "424B3", "424B5",
}
US_WATCHLIST = (
    "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "AMD", "TSLA",
    "AVGO", "NFLX", "CRM", "ORCL", "ADBE", "INTC", "QCOM", "TXN",
    "AMAT", "PYPL", "COIN", "NOW", "PLTR", "SNOW", "MU", "LRCX",
    "ASML", "JPM", "BAC", "XOM", "V", "DIS",
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            count += 1
    return count


def _next_weekday(day: date) -> date:
    candidate = day + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _date_string(value: Any, fallback: date) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    match = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", str(value or ""))
    if match:
        return "-".join(match.groups())
    return fallback.isoformat()


def _cn_symbol(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-6:].zfill(6) if digits else ""


def _classify_cn(title: str, announcement_type: str) -> tuple[str | None, int, list[str]]:
    text = f"{title} {announcement_type}"
    reasons: list[str] = []
    if CN_GUIDANCE.search(text):
        return "公司指引上调/下调", 5, reasons
    if CN_EARNINGS.search(text):
        if re.search(r"预增|预减|扭亏|续盈|续亏|首亏|增亏", text):
            score = 5
        elif re.search(r"业绩预告|业绩快报", text):
            score = 4
        else:
            score = 2
        if LOW_VALUE_CN.search(text):
            score = min(score, 1)
            reasons.append("low_information_document_type")
        return "财报超预期/不及预期", score, reasons
    if CN_MA.search(text):
        if re.search(r"要约收购报告书|重大资产重组报告书|收购报告书|重组预案", text):
            score = 5
        elif re.search(r"发行股份购买资产|定增预案|非公开发行|配股|可转债", text):
            score = 4
        else:
            score = 2
        if LOW_VALUE_CN.search(text):
            score = 1
            reasons.append("low_information_document_type")
        return "并购/分拆/再融资", score, reasons
    return None, 0, reasons


def _notice_code(url: str) -> str:
    match = re.search(r"/(AN\d+)\.html", url or "", flags=re.IGNORECASE)
    return match.group(1).upper() if match else ""


def _fetch_notice_content(art_code: str) -> dict[str, Any]:
    if not art_code:
        return {"ok": False, "error": "missing_notice_code", "content": ""}
    endpoint = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
    pages: list[str] = []
    first: dict[str, Any] = {}
    last_error: str | None = None
    for page_index in range(1, 51):
        data: dict[str, Any] | None = None
        for attempt in range(3):
            try:
                response = requests.get(
                    endpoint,
                    params={
                        "art_code": art_code,
                        "client_source": "web",
                        "page_index": page_index,
                    },
                    headers=headers,
                    timeout=(6, 20),
                )
                response.raise_for_status()
                data = response.json().get("data") or {}
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < 2:
                    time_module.sleep(0.5 * (attempt + 1))
        if data is None:
            break
        if page_index == 1:
            first = data
        content = str(data.get("notice_content") or "").strip()
        if content:
            pages.append(content)
        page_size = int(data.get("page_size") or 1)
        if page_index >= page_size:
            break
        time_module.sleep(0.1)
    return {
        "ok": bool(pages),
        "error": last_error or (None if pages else "empty_notice_content"),
        "content": "\n\n".join(pages),
        "notice_title": first.get("notice_title"),
        "source_published_at": first.get("eitime") or first.get("notice_date"),
        "attachment_url": first.get("attach_url_web") or first.get("attach_url"),
        "pages": int(first.get("page_size") or len(pages) or 0),
    }


def collect_cn(target: date, collected_at: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    frame = ak.stock_notice_report(symbol="全部", date=target.strftime("%Y%m%d"))
    raw_rows: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        symbol = _cn_symbol(row.get("代码"))
        name = str(row.get("名称") or "").strip()
        title = str(row.get("公告标题") or "").strip()
        ann_type = str(row.get("公告类型") or "").strip()
        source_url = str(row.get("网址") or "").strip()
        event_date = _date_string(row.get("公告日期"), target)
        raw = {
            "schema_version": SCHEMA_VERSION,
            "source": "akshare.stock_notice_report",
            "collected_at": collected_at.isoformat(),
            "target_date": target.isoformat(),
            "market": "CN",
            "symbol": symbol,
            "issuer_name": name,
            "announcement_date": event_date,
            "announcement_type": ann_type,
            "title": title,
            "source_url": source_url,
            "source_notice_code": _notice_code(source_url),
        }
        raw_rows.append(raw)
        event_type, quality_score, initial_reasons = _classify_cn(title, ann_type)
        if not event_type:
            continue
        candidates.append({
            **raw,
            "event_type_l2": event_type,
            "quality_score": quality_score,
            "initial_quality_reasons": initial_reasons,
        })
    return raw_rows, candidates


def _sec_event_type(form: str, items: str) -> str | None:
    if form in {"10-Q", "10-K", "10-Q/A", "10-K/A", "20-F", "40-F"}:
        return "财报超预期/不及预期"
    if form == "8-K" and "2.02" in items:
        return "财报超预期/不及预期"
    if form in {"425", "S-4", "S-4/A", "DEFM14A", "PREM14A"}:
        return "并购/分拆/再融资"
    if form == "8-K" and any(item in items for item in ("1.01", "1.02", "2.01")):
        return "并购/分拆/再融资"
    return None


def _fetch_sec_document(url: str, *, include_exhibits: bool = False) -> dict[str, Any]:
    headers = {"User-Agent": "Pronoia online-test research@example.com"}
    last_error: str | None = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers, timeout=(6, 30))
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if "html" in content_type or url.lower().endswith((".htm", ".html")):
                soup = BeautifulSoup(response.content, "lxml")
                text_parts = [soup.get_text(" ", strip=True)]
                if include_exhibits:
                    exhibit_urls: list[str] = []
                    for link in soup.find_all("a", href=True):
                        label = link.get_text(" ", strip=True)
                        href = str(link.get("href") or "")
                        if re.search(r"(?:^|\D)99\.1(?:\D|$)|earnings|ex99", f"{label} {href}", re.I):
                            exhibit_url = urljoin(url, href)
                            if exhibit_url not in exhibit_urls:
                                exhibit_urls.append(exhibit_url)
                    for exhibit_url in exhibit_urls[:3]:
                        exhibit = _fetch_sec_document(exhibit_url)
                        if exhibit.get("ok"):
                            text_parts.append(str(exhibit.get("content") or ""))
                text = "\n\n".join(part for part in text_parts if part)
            else:
                text = response.text.strip()
            return {
                "ok": len(text) >= 160,
                "error": None if text else "empty_sec_document",
                "content": text,
            }
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                time_module.sleep(0.5 * (attempt + 1))
    return {"ok": False, "error": last_error, "content": ""}


def collect_us(target: date, collected_at: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    raw_rows: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    headers = {"User-Agent": "Pronoia online-test research@example.com"}
    try:
        ticker_response = requests.get(
            "https://www.sec.gov/files/company_tickers.json", headers=headers, timeout=(4, 20)
        )
        ticker_response.raise_for_status()
        ticker_payload = ticker_response.json()
        ticker_to_cik = {
            str(v.get("ticker") or "").upper(): int(v.get("cik_str"))
            for v in ticker_payload.values()
            if v.get("ticker") and v.get("cik_str") is not None
        }
    except Exception as exc:  # noqa: BLE001
        return [], [], [f"sec_ticker_map: {type(exc).__name__}: {exc}"]

    for symbol in US_WATCHLIST:
        cik = ticker_to_cik.get(symbol)
        if cik is None:
            continue
        try:
            response = requests.get(
                f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
                headers=headers,
                timeout=(4, 20),
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"sec_submissions:{symbol}: {type(exc).__name__}: {exc}")
            continue
        recent = (payload.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        items_values = recent.get("items") or []
        accession_values = recent.get("accessionNumber") or []
        documents = recent.get("primaryDocument") or []
        company_name = str(payload.get("name") or symbol).strip()
        for index, form_value in enumerate(forms):
            filing_date = str(dates[index] if index < len(dates) else "")[:10]
            if filing_date != target.isoformat():
                continue
            form = str(form_value or "").strip()
            items = str(items_values[index] if index < len(items_values) else "").strip()
            accession = str(accession_values[index] if index < len(accession_values) else "").strip()
            document = str(documents[index] if index < len(documents) else "").strip()
            accession_flat = accession.replace("-", "")
            source_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_flat}/{document}"
                if accession and document else
                f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
            )
            raw = {
                "schema_version": SCHEMA_VERSION,
                "source": "sec.edgar.submissions",
                "collected_at": collected_at.isoformat(),
                "target_date": target.isoformat(),
                "market": "US",
                "symbol": symbol,
                "issuer_name": company_name,
                "announcement_date": filing_date,
                "announcement_type": form,
                "title": f"{company_name} {form}",
                "source_url": source_url,
                "source_notice_code": accession,
                "sec_items": items,
            }
            raw_rows.append(raw)
            event_type = _sec_event_type(form, items) if form in SEC_FORMS else None
            if event_type:
                document = _fetch_sec_document(
                    source_url,
                    include_exhibits=form == "8-K" and "2.02" in items,
                )
                candidates.append({
                    **raw,
                    "event_type_l2": event_type,
                    "quality_score": 5 if form != "8-K" else 4,
                    "initial_quality_reasons": [],
                    "document_content": document.get("content") or "",
                    "document_fetch_ok": bool(document.get("ok")),
                    "document_fetch_error": document.get("error"),
                })
        time_module.sleep(0.11)
    return raw_rows, candidates, errors


def collect_macro(target: date, collected_at: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    try:
        frame = ak.news_economic_baidu()
    except Exception as exc:  # noqa: BLE001
        return [], [], [f"macro_calendar: {type(exc).__name__}: {exc}"]
    raw_rows: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        row_date = _date_string(row.get("日期"), target)
        if row_date != target.isoformat():
            continue
        region = str(row.get("地区") or "").strip()
        name = str(row.get("事件") or "").strip()
        actual = str(row.get("公布") or "").strip()
        consensus = str(row.get("预期") or "").strip()
        previous = str(row.get("前值") or "").strip()
        lowered = name.lower()
        if re.search(r"cpi|ppi|pce|inflation|通胀|物价", lowered):
            event_type = "通胀数据意外"
        elif re.search(r"非农|失业率|employment|payroll|unemployment|pmi|gdp|零售|工业增加值", lowered):
            event_type = "增长/就业数据意外"
        elif re.search(r"rate decision|fomc|fed funds|lpr|mlf|议息|降息|加息", lowered):
            event_type = "政策利率调整"
        else:
            event_type = None
        if region in {"中国", "CN", "China"}:
            market, symbol, benchmark = "CN", "sh000300", CN_BENCHMARK
        elif region in {"美国", "US", "United States"}:
            market, symbol, benchmark = "US", "SPY", US_BENCHMARK
        else:
            continue
        raw = {
            "schema_version": SCHEMA_VERSION,
            "source": "akshare.news_economic_baidu",
            "collected_at": collected_at.isoformat(),
            "target_date": target.isoformat(),
            "market": market,
            "symbol": symbol,
            "issuer_name": region,
            "announcement_date": row_date,
            "announcement_type": "macro_release",
            "title": f"【{region}】{name}",
            "source_url": "akshare.news_economic_baidu",
            "source_notice_code": _sha256_text(f"{region}|{row_date}|{name}")[:16],
            "actual": actual,
            "consensus": consensus,
            "previous": previous,
        }
        raw_rows.append(raw)
        if event_type:
            candidates.append({
                **raw,
                "event_type_l2": event_type,
                "quality_score": 5 if actual and consensus else 2,
                "initial_quality_reasons": [] if actual and consensus else ["missing_actual_or_consensus"],
                "document_content": f"{name} | 公布:{actual} 预期:{consensus} 前值:{previous}",
                "document_fetch_ok": True,
                "benchmark": benchmark,
            })
    return raw_rows, candidates, []


def enrich_cn_candidates(candidates: list[dict[str, Any]]) -> None:
    for row in candidates:
        # Low-confidence title matches are quarantined later. Fetching dozens of
        # long PDFs for those rows adds latency without making them eligible.
        if int(row.get("quality_score") or 0) < 3:
            row["document_fetch_ok"] = False
            row["document_fetch_error"] = "skipped_below_quality_threshold"
            row["document_content"] = ""
            continue
        detail = _fetch_notice_content(str(row.get("source_notice_code") or ""))
        row["document_fetch_ok"] = bool(detail.get("ok"))
        row["document_fetch_error"] = detail.get("error")
        row["document_content"] = str(detail.get("content") or "")
        row["source_published_at"] = detail.get("source_published_at")
        row["attachment_url"] = detail.get("attachment_url")
        row["document_pages"] = detail.get("pages")


def validate_candidate(row: dict[str, Any], collected_at: datetime, target: date) -> tuple[dict[str, Any], list[str]]:
    reasons = list(row.get("initial_quality_reasons") or [])
    market = str(row.get("market") or "")
    symbol = str(row.get("symbol") or "")
    title = str(row.get("title") or "").strip()
    issuer = str(row.get("issuer_name") or "").strip()
    content = str(row.get("document_content") or "").strip()
    source_url = str(row.get("source_url") or "").strip()
    event_type = str(row.get("event_type_l2") or "")

    try:
        ref = resolve_security_ref(symbol, market)
        resolved_symbol = ref.symbol
        security_kind = ref.kind
    except Exception as exc:  # noqa: BLE001
        resolved_symbol = ""
        security_kind = ""
        reasons.append(f"invalid_symbol:{type(exc).__name__}")
    if event_type not in EVENT_TYPES:
        reasons.append("unsupported_event_type")
    if not title:
        reasons.append("missing_title")
    if not source_url or (source_url.startswith("http") and not urlparse(source_url).netloc):
        reasons.append("invalid_source_url")
    fetch_error = str(row.get("document_fetch_error") or "")
    if not row.get("document_fetch_ok"):
        if fetch_error == "skipped_below_quality_threshold":
            reasons.append("document_fetch_skipped_below_quality_threshold")
        else:
            reasons.append("document_fetch_failed")
    if int(row.get("quality_score") or 0) >= 3 and len(content) < 160:
        reasons.append("document_too_short")
    if int(row.get("quality_score") or 0) < 3:
        reasons.append("quality_score_below_3")
    if market == "CN" and content:
        identity_ok = symbol in content or (issuer and issuer in content)
        if not identity_ok:
            reasons.append("issuer_symbol_content_mismatch")

    effective_session = _next_weekday(target)
    if market == "CN":
        cutoff = datetime.combine(effective_session, time(9, 15), tzinfo=CN_TZ)
    else:
        cutoff = datetime.combine(effective_session, time(9, 15), tzinfo=US_TZ)
    prediction_eligible = collected_at.astimezone(cutoff.tzinfo) < cutoff
    if not prediction_eligible:
        reasons_for_prediction = ["prediction_cutoff_missed"]
    else:
        reasons_for_prediction = []

    unique_source_id = str(row.get("source_notice_code") or source_url)
    event_id = (
        f"online_{target.strftime('%Y%m%d')}_{market.lower()}_"
        f"{symbol}_{hashlib.sha1(unique_source_id.encode('utf-8')).hexdigest()[:10]}"
    )
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "cohort_date": target.isoformat(),
        "event_id": event_id,
        "market": market,
        "symbol": symbol,
        "resolved_symbol": resolved_symbol,
        "security_kind": security_kind,
        "issuer_name": issuer,
        "published_at": row.get("source_published_at") or row.get("announcement_date"),
        "collected_at": collected_at.isoformat(),
        "effective_session": effective_session.isoformat(),
        "prediction_cutoff_at": cutoff.isoformat(),
        "event_time": str(row.get("announcement_date") or target.isoformat()),
        "event_type_l2": event_type,
        "title": title,
        "event_text": content,
        "source_url": source_url,
        "attachment_url": row.get("attachment_url"),
        "source_notice_code": row.get("source_notice_code"),
        "source": row.get("source"),
        "related_documents": row.get("related_documents") or [],
        "source_document_count": int(row.get("source_document_count") or 1),
        "source_document_hash": _sha256_text(content) if content else None,
        "benchmark": row.get("benchmark") or (CN_BENCHMARK if market == "CN" else US_BENCHMARK),
        "sector_etf": None,
        "direction_prior": None,
        "event_strength": None,
        "quality_score": int(row.get("quality_score") or 0),
        "quality_pass": not reasons,
        "quality_reasons": sorted(set(reasons)),
        "prediction_eligible": prediction_eligible and not reasons,
        "prediction_eligibility_reasons": reasons_for_prediction,
    }
    return normalized, reasons


def _dedupe_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_source: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("source") or ""),
            str(row.get("source_notice_code") or row.get("source_url") or ""),
        )
        current = by_source.get(key)
        if current is None or int(row.get("quality_score") or 0) > int(current.get("quality_score") or 0):
            by_source[key] = row

    # One corporate event often produces a report, an adviser opinion and a
    # reminder notice. Keep one model input while retaining all source links.
    family_patterns = (
        ("要约收购", r"要约收购"),
        ("控制权收购", r"收购报告书"),
        ("重大资产重组", r"重大资产重组|发行股份购买资产|吸收合并"),
        ("分拆", r"分拆"),
        ("再融资", r"向特定对象发行|非公开发行|定增|配股|可转债"),
        ("业绩预告", r"业绩预告|预增|预减|扭亏|续盈|续亏|首亏|增亏"),
        ("业绩快报", r"业绩快报"),
    )

    def event_family(row: dict[str, Any]) -> str:
        title = str(row.get("title") or "")
        for family, pattern in family_patterns:
            if re.search(pattern, title):
                return family
        return str(row.get("source_notice_code") or row.get("source_url") or title)

    grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for row in by_source.values():
        key = (
            str(row.get("market") or ""),
            str(row.get("symbol") or ""),
            str(row.get("announcement_date") or ""),
            str(row.get("event_type_l2") or ""),
            event_family(row),
        )
        grouped.setdefault(key, []).append(row)

    def primary_score(row: dict[str, Any]) -> int:
        title = str(row.get("title") or "")
        auxiliary = bool(re.search(r"财务顾问意见|法律意见|审计报告|评估报告|问询函|提示性公告", title))
        fetch_penalty = 100 if not row.get("document_fetch_ok") else 0
        return int(row.get("quality_score") or 0) * 10 - (5 if auxiliary else 0) - fetch_penalty

    deduped: list[dict[str, Any]] = []
    for members in grouped.values():
        primary = max(members, key=primary_score).copy()
        primary["related_documents"] = [
            {
                "title": item.get("title"),
                "source_url": item.get("source_url"),
                "source_notice_code": item.get("source_notice_code"),
                "document_fetch_ok": item.get("document_fetch_ok"),
            }
            for item in members
        ]
        primary["source_document_count"] = len(members)
        deduped.append(primary)
    return deduped


def build_report(
    *, target: date, collected_at: datetime, raw: list[dict[str, Any]],
    candidates: list[dict[str, Any]], validated: list[dict[str, Any]],
    quarantine: list[dict[str, Any]], eligible: list[dict[str, Any]],
    source_errors: list[str],
) -> str:
    by_source = Counter(str(r.get("source") or "unknown") for r in raw)
    source_counts = {
        "akshare.stock_notice_report": by_source.get("akshare.stock_notice_report", 0),
        "sec.edgar.submissions": by_source.get("sec.edgar.submissions", 0),
        "akshare.news_economic_baidu": by_source.get("akshare.news_economic_baidu", 0),
    }
    by_type = Counter(str(r.get("event_type_l2") or "unknown") for r in candidates)
    quarantine_reasons = Counter(
        reason for row in quarantine for reason in row.get("quality_reasons") or []
    )
    lines = [
        f"# Online test 初步采集报告：{target.isoformat()}",
        "",
        f"- 采集时间：`{collected_at.isoformat()}`",
        f"- 原始记录：**{len(raw)}**",
        f"- 六类事件候选：**{len(candidates)}**",
        f"- 质量验证通过：**{len(validated)}**",
        f"- Quarantine：**{len(quarantine)}**",
        f"- 可作为无泄漏在线预测：**{len(eligible)}**",
        "",
        "> 这是对昨日事件的回填采集。若采集时间晚于下一交易日预测截止点，",
        "> 即使 packet 质量合格，也不会进入正式 online ACC 分母。",
        "",
        "## 数据源",
        "",
        "| 数据源 | 原始记录 |",
        "|---|---:|",
    ]
    lines.extend(f"| `{source}` | {count} |" for source, count in source_counts.items())
    lines.extend(["", "## 候选事件类型", "", "| 类型 | 数量 |", "|---|---:|"])
    lines.extend(f"| {etype} | {count} |" for etype, count in sorted(by_type.items()))
    lines.extend(["", "## 验证通过", ""])
    if validated:
        lines.extend(["| Event | 标的 | 类型 | 质量分 | 正式预测资格 |", "|---|---|---|---:|---|"])
        for row in validated:
            lines.append(
                f"| {row['title']} | {row['market']} {row['symbol']} | "
                f"{row['event_type_l2']} | {row['quality_score']} | "
                f"{'是' if row['prediction_eligible'] else '否（已错过截止点）'} |"
            )
    else:
        lines.append("无。")
    lines.extend(["", "## Quarantine 原因", "", "| 原因 | 次数 |", "|---|---:|"])
    if quarantine_reasons:
        lines.extend(f"| `{reason}` | {count} |" for reason, count in quarantine_reasons.most_common())
    else:
        lines.append("| — | 0 |")
    lines.extend(["", "## 数据源错误", ""])
    if source_errors:
        lines.extend(f"- `{error}`" for error in source_errors)
    else:
        lines.append("无。")
    lines.extend([
        "",
        "## 结论",
        "",
        "本批次用于验证采集、正文补齐、身份校验和冻结流程。由于是昨日回填，",
        "不会把已错过预测截止时间的样本计入正式 online test 准确率。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date",
        default=(datetime.now(CN_TZ).date() - timedelta(days=1)).isoformat(),
        help="目标公告日期，YYYY-MM-DD；默认昨日",
    )
    parser.add_argument(
        "--output-root",
        default=str(PROJECT_ROOT / "pronoia_run" / "online_test"),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖已冻结的同日期 cohort；仅用于显式重建",
    )
    args = parser.parse_args()
    target = date.fromisoformat(args.date)
    collected_at = datetime.now(CN_TZ)
    out_dir = Path(args.output_root) / target.isoformat()
    if (out_dir / "manifest.json").exists() and not args.overwrite:
        raise SystemExit(
            f"cohort is already frozen: {out_dir}; pass --overwrite to rebuild explicitly"
        )
    out_dir.mkdir(parents=True, exist_ok=True)

    source_errors: list[str] = []
    cn_raw, cn_candidates = collect_cn(target, collected_at)
    enrich_cn_candidates(cn_candidates)
    us_raw, us_candidates, us_errors = collect_us(target, collected_at)
    macro_raw, macro_candidates, macro_errors = collect_macro(target, collected_at)
    source_errors.extend(us_errors)
    source_errors.extend(macro_errors)

    raw = cn_raw + us_raw + macro_raw
    candidates = _dedupe_candidates(cn_candidates + us_candidates + macro_candidates)
    validated: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    for candidate in candidates:
        normalized, reasons = validate_candidate(candidate, collected_at, target)
        if reasons:
            quarantine.append(normalized)
        else:
            validated.append(normalized)
    eligible = [row for row in validated if row.get("prediction_eligible")]

    paths = {
        "raw": out_dir / "raw.jsonl",
        "candidates": out_dir / "candidates.jsonl",
        "validated": out_dir / "validated.jsonl",
        "quarantine": out_dir / "quarantine.jsonl",
        "prediction_eligible": out_dir / "prediction_eligible.jsonl",
    }
    _write_jsonl(paths["raw"], raw)
    _write_jsonl(paths["candidates"], candidates)
    _write_jsonl(paths["validated"], validated)
    _write_jsonl(paths["quarantine"], quarantine)
    _write_jsonl(paths["prediction_eligible"], eligible)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "cohort_date": target.isoformat(),
        "collected_at": collected_at.isoformat(),
        "mode": "backfill" if target < collected_at.date() else "forward",
        "frozen": True,
        "counts": {
            "raw": len(raw),
            "candidates": len(candidates),
            "validated": len(validated),
            "quarantine": len(quarantine),
            "prediction_eligible": len(eligible),
        },
        "by_market_raw": dict(Counter(str(r.get("market") or "unknown") for r in raw)),
        "by_type_candidates": dict(Counter(str(r.get("event_type_l2") or "unknown") for r in candidates)),
        "source_errors": source_errors,
        "files": {
            name: {"path": path.name, "sha256": _sha256_file(path)}
            for name, path in paths.items()
        },
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path = out_dir / "report.md"
    report_path.write_text(
        build_report(
            target=target,
            collected_at=collected_at,
            raw=raw,
            candidates=candidates,
            validated=validated,
            quarantine=quarantine,
            eligible=eligible,
            source_errors=source_errors,
        ),
        encoding="utf-8",
    )
    print(json.dumps({**manifest["counts"], "output_dir": str(out_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
