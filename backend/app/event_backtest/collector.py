from __future__ import annotations

import ctypes
import hashlib
import logging
import queue
import re
import threading
from typing import Iterable

import akshare as ak
import pandas as pd

from ..skills.market import get_us_stock_sec_filings
from ..skills.news import get_announcements
from .models import EventRecord

log = logging.getLogger(__name__)

_COLLECT_HTTP_TIMEOUT_S = 10
_COLLECT_US_HTTP_TIMEOUT_S = 20
_COLLECT_MAX_RETRY = 3


class _TimeoutKill(Exception):
    """Marker raised inside a worker thread if the watchdog decides to kill it."""


def _call_with_timeout(fn, *, timeout_s: int, label: str):
    """Run `fn()` in a worker thread; return None if it takes longer than `timeout_s`.

    akshare / yfinance 内部 HTTP 调用经常没设 timeout，TCP 黑盒卡死时 STAT=S+、
    CPU TIME 不增长，wall-clock 却一直走。这里：
      - 先尝试软 timeout（concurrent.futures future.result(timeout=…)），
        让调用方立刻返回 None，不再等待那个卡住的线程；
      - 同时设置线程为 daemon，避免“老线程悬着不退出 + ThreadPoolExecutor 强 join”把主程序挂死。
      - 如果连续多次卡死，下一轮自然由 max_retry 跳过。
    """
    t = max(1, int(timeout_s or _COLLECT_HTTP_TIMEOUT_S))
    result_q: queue.Queue = queue.Queue(maxsize=1)
    exc_q: queue.Queue = queue.Queue(maxsize=1)

    def worker():
        try:
            result_q.put(fn())
        except Exception as e:  # noqa: BLE001
            exc_q.put(e)

    th = threading.Thread(target=worker, name=f"collect-{label}", daemon=True)
    th.start()
    th.join(timeout=t)
    if th.is_alive():
        log.warning("[collect] timeout %ss on %s (worker still running, skipping)", t, label)
        # NOTE: 我们不会强制 kill，也不会再等它；线程会被 OS 在整个进程退出时回收。
        # 但由于 daemon=True，它不会阻止主程序退出。
        return None
    if not exc_q.empty():
        e = exc_q.get()
        log.warning("[collect] error on %s: %s: %s", label, type(e).__name__, e)
        return None
    if result_q.empty():
        return None
    return result_q.get()


def _retry_with_timeout(fn, *, timeout_s: int = _COLLECT_HTTP_TIMEOUT_S,
                        max_retry: int = _COLLECT_MAX_RETRY, label: str):
    last_err = None
    for attempt in range(1, max(1, int(max_retry or 1)) + 1):
        try:
            res = _call_with_timeout(fn, timeout_s=timeout_s, label=f"{label}#try{attempt}")
            if res is not None:
                return res
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("[collect] attempt %s fail on %s: %s: %s", attempt, label, type(e).__name__, e)
    if last_err is not None:
        log.warning("[collect] all attempts fail on %s: %s", label, last_err)
    return None


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or "event"


def _digest(*parts: str) -> str:
    base = "||".join(str(x or "") for x in parts)
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:10]


def _match_cn_type(title: str, snippet: str) -> str | None:
    text = f"{title} {snippet}"
    if re.search(r"(回购|股份回购)", text):
        return "股份回购"
    if re.search(r"(减持|减持计划|减持股份)", text):
        return "股东减持"
    if re.search(r"(收购|并购|重组|分拆|定增|增发|配股|可转债|再融资)", text):
        return "并购/分拆/再融资"
    if re.search(r"(业绩预告|业绩快报|年报|半年报|中报|一季报|三季报|季度报告|财报)", text):
        return "财报超预期/不及预期"
    if re.search(r"(盈利预测|业绩指引|业绩展望|经营目标|年度目标|上调.*?(预期|预测|指引)|下调.*?(预期|预测|指引))", text):
        return "公司指引上调/下调"
    return None


def _match_us_type(filing_type: str, title: str) -> str | None:
    text = f"{filing_type} {title}".lower()
    if re.search(r"(merger|acquisition|acquire|spin[- ]?off|divestiture|take-private|business combination)", text):
        return "并购/分拆/再融资"
    if filing_type in {"10-k", "10-q", "10-k/a", "10-q/a", "20-f"} or re.search(
        r"(earnings|financial results|quarterly results|annual results|results for the quarter|results for the year)",
        text,
    ):
        return "财报超预期/不及预期"
    if re.search(r"(guidance|outlook|forecast|reaffirms|raises guidance|lowers guidance|updates outlook)", text):
        return "公司指引上调/下调"
    return None


def _normalize_event(
    *,
    market: str,
    symbol: str,
    event_time: str,
    event_type_l2: str,
    title: str,
    event_text: str,
    source_url: str,
    sector_etf: str | None = None,
    benchmark: str | None = None,
    direction_prior: str | None = None,
    event_strength: int | None = None,
) -> EventRecord:
    event_id = f"seed_{market.lower()}_{_slug(symbol)}_{_slug(event_type_l2)}_{_digest(symbol, event_time, title, source_url)}"
    return EventRecord(
        event_id=event_id,
        market=market.upper(),  # type: ignore[arg-type]
        symbol=symbol,
        event_time=event_time,
        event_type_l2=event_type_l2,
        title=title.strip(),
        event_text=event_text.strip() or title.strip(),
        source_url=source_url.strip(),
        sector_etf=sector_etf,
        benchmark=benchmark,
        direction_prior=direction_prior,
        event_strength=event_strength,
    )


def collect_cn_announcement_seeds(
    *,
    dates: Iterable[str],
    keywords: Iterable[str] | None = None,
    limit_per_query: int = 30,
    diagnostics: list[dict] | None = None,
) -> list[EventRecord]:
    out: list[EventRecord] = []
    seen: set[tuple[str, str]] = set()
    kw_list = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not kw_list:
        kw_list = ["并购", "重组", "收购", "业绩", "快报", "预告", "指引", "定增"]
    for date in dates:
        for kw in kw_list:
            label = f"cn_ak:{date}:{kw}"
            res = _retry_with_timeout(
                lambda d=date, k=kw: get_announcements(date=str(d), keyword=k),
                label=label,
            )
            if res is None:
                if diagnostics is not None:
                    diagnostics.append({"query": label, "status": "failed", "message": "request failed or timed out"})
                continue
            if isinstance(res, dict) and res.get("ok") is False:
                # skill.err 返回：打印警告但不挂整条链路
                log.debug("[collect] %s skill.err skip: %s", label, res.get("message"))
                if diagnostics is not None:
                    diagnostics.append({"query": label, "status": "failed", "message": res.get("message") or res.get("error")})
                continue
            items = (res or {}).get("data") or []
            if not isinstance(items, list):
                if diagnostics is not None:
                    diagnostics.append({"query": label, "status": "failed", "message": "invalid response payload"})
                continue
            if diagnostics is not None:
                diagnostics.append({"query": label, "status": "ok" if items else "empty", "count": len(items), "note": (res or {}).get("note")})
            for raw in items[: max(1, int(limit_per_query or 30))]:
                if not isinstance(raw, dict):
                    continue
                title = str(raw.get("title") or "").strip()
                snippet = str(raw.get("snippet") or "").strip()
                url = str(raw.get("url") or "").strip()
                etype = _match_cn_type(title, snippet)
                if not title or not url or not etype:
                    continue
                key = (title, url)
                if key in seen:
                    continue
                seen.add(key)
                code_match = re.search(r"\b(\d{6})\b", snippet)
                symbol = code_match.group(1) if code_match else "UNKNOWN"
                event_time = str(raw.get("date") or date).strip()
                out.append(
                    _normalize_event(
                        market="CN",
                        symbol=symbol,
                        event_time=event_time,
                        event_type_l2=etype,
                        title=title,
                        event_text=f"{title} | {snippet}",
                        source_url=url,
                        benchmark="sh000300",
                    )
                )
    return out


def collect_us_sec_seeds(
    *,
    symbols: Iterable[str],
    count_per_symbol: int = 20,
    diagnostics: list[dict] | None = None,
) -> list[EventRecord]:
    out: list[EventRecord] = []
    seen: set[tuple[str, str]] = set()
    for symbol in symbols:
        sym = str(symbol).strip().upper()
        if not sym:
            continue
        label = f"us_sec:{sym}"
        res = _retry_with_timeout(
            lambda s=sym, n=count_per_symbol: get_us_stock_sec_filings(symbol=s, count=n),
            label=label,
            timeout_s=_COLLECT_US_HTTP_TIMEOUT_S,
            max_retry=_COLLECT_MAX_RETRY,
        )
        if res is None:
            if diagnostics is not None:
                diagnostics.append({"query": label, "status": "failed", "message": "request failed or timed out"})
            continue
        if isinstance(res, dict) and res.get("ok") is False:
            log.debug("[collect] %s skill.err skip: %s", label, res.get("message"))
            if diagnostics is not None:
                diagnostics.append({"query": label, "status": "failed", "message": res.get("message") or res.get("error")})
            continue
        payload = (res or {}).get("data") or {}
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            if diagnostics is not None:
                diagnostics.append({"query": label, "status": "failed", "message": "invalid response payload"})
            continue
        if diagnostics is not None:
            diagnostics.append({"query": label, "status": "ok" if rows else "empty", "count": len(rows)})
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            filing_type = str(raw.get("type") or "").strip()
            title = str(raw.get("title") or "").strip()
            url = str(raw.get("edgar_url") or "").strip()
            etype = _match_us_type(filing_type, title)
            if not title or not url or not etype:
                continue
            key = (title, url)
            if key in seen:
                continue
            seen.add(key)
            event_time = str(raw.get("date") or "").strip()
            benchmark = "QQQ" if re.search(r"(technology|ai|gpu|semiconductor|software|cloud)", title.lower()) else "SPY"
            out.append(
                _normalize_event(
                    market="US",
                    symbol=sym,
                    event_time=event_time,
                    event_type_l2=etype,
                    title=title,
                    event_text=f"{filing_type} | {title}",
                    source_url=url,
                    benchmark=benchmark,
                )
            )
    return out


def _macro_event_type(event_name: str) -> str | None:
    e = (event_name or "").strip().lower()
    if not e:
        return None
    if re.search(r"(cpi|ppi|pce|inflation|consumer price|producer price|通胀|物价)", e):
        return "通胀数据意外"
    if re.search(r"(非农|失业率|employment|payrolls|unemployment|job)", e):
        return "增长/就业数据意外"
    if re.search(r"(pmi|gdp|工业增加值|零售|retail sales|ism|industrial production)", e):
        return "增长/就业数据意外"
    if re.search(r"(rate decision|interest rate decision|fomc|fed funds|lpr|mlf|loan prime rate|议息|基准利率|降息|加息)", e):
        return "政策利率调整"
    return None


def collect_macro_calendar_seeds(*, limit: int = 120) -> list[EventRecord]:
    def _fetch() -> pd.DataFrame | None:
        df = ak.news_economic_baidu()
        if df is None:
            return None
        return df.reset_index(drop=True)

    df = _retry_with_timeout(_fetch, label="macro_economic_baidu", timeout_s=max(30, _COLLECT_HTTP_TIMEOUT_S))
    if not isinstance(df, pd.DataFrame) or len(df) == 0:
        return []
    df = df.tail(max(1, int(limit or 120)))
    out: list[EventRecord] = []
    seen: set[tuple[str, str, str]] = set()
    for _, r in df.iterrows():
        region = str(r.get("地区") or "").strip()
        event_name = str(r.get("事件") or "").strip()
        if not region or not event_name:
            continue
        etype = _macro_event_type(event_name)
        if not etype:
            continue
        date = str(r.get("日期") or "").strip()
        time = str(r.get("时间") or "").strip()
        event_time = f"{date} {time}".strip()
        actual = str(r.get("公布") or "").strip()
        consensus = str(r.get("预期") or "").strip()
        prev = str(r.get("前值") or "").strip()
        if region in {"美国", "US", "United States"}:
            market = "US"
            symbol = "SPY"
            benchmark = "SPY"
        elif region in {"中国", "CN", "China"}:
            market = "CN"
            symbol = "sh000300"
            benchmark = "sh000300"
        else:
            continue
        key = (market, event_time, event_name)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            _normalize_event(
                market=market,
                symbol=symbol,
                event_time=event_time,
                event_type_l2=etype,
                title=f"【{region}】{event_name}",
                event_text=f"{event_name} | 公布:{actual} 预期:{consensus} 前值:{prev}".strip(),
                source_url="akshare.news_economic_baidu",
                benchmark=benchmark,
            )
        )
    return out
