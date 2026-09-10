"""Analysis skills: event_study — 事件研究法 (design.md §5).

取事件日前后 [-pre, +post] 交易日的个股日K与指数日K（向前多取缓冲保证窗口），
计算日收益 r_stock / r_index，AR_t = r_stock - r_index，CAR_t = ΣAR（自 -pre 起累计）。
所有收益类字段单位：%（百分比）。

支持 A 股（6 位代码 + 沪深 300 等指数）与 美股（ticker + SPY/QQQ 等 ETF）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Optional
import math

import pandas as pd

from .market import (
    is_a_share_index_symbol,
    is_us_symbol,
    norm_date,
    norm_index_symbol,
    norm_symbol,
)
from .registry import err, meta, ok, skill
from . import cache
from .price_data import fetch_price_frame


@cache.cached("kline")
def _fetch_close_payload(
    symbol: str,
    start8: str,
    end8: str,
    market: str,
) -> dict:
    """Fetch and cache one bounded close series through the shared provider router."""
    price = fetch_price_frame(
        symbol,
        start_date=start8,
        end_date=end8,
        market=market,
        adjust="qfq",
    )
    rows = price.frame[["date", "close"]].to_dict(orient="records")
    return {
        "ok": True,
        "data": {
            "rows": rows,
            "provider": price.provider,
            "attempts": list(price.attempts),
            "security": price.security.as_dict(),
        },
        "meta": meta(price.provider, len(rows)),
    }


def _payload_to_close(payload: dict, symbol: str) -> tuple[pd.DataFrame, str, list[str]]:
    data = payload.get("data") or {}
    rows = data.get("rows") or []
    if not rows:
        raise ValueError(f"{symbol} 无行情数据")
    frame = pd.DataFrame(rows)
    if "date" not in frame.columns or "close" not in frame.columns:
        raise ValueError(f"{symbol} 行情缺少 date/close 字段")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = (
        frame.dropna(subset=["date", "close"])
        .drop_duplicates(subset=["date"], keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )
    if frame.empty:
        raise ValueError(f"{symbol} 无有效行情数据")
    return (
        frame[["date", "close"]],
        str(data.get("provider") or "unknown"),
        [str(item) for item in data.get("attempts") or []],
    )


@skill(
    "event_study",
    "事件研究法：以事件日为 T0，计算窗口 [-pre,+post] 内个股相对指数的超额收益 AR 与累计超额收益 CAR，"
    "并给出事件日前5日/后5日累计收益、CAR终值、事件日涨跌幅。收益单位%。"
    "支持 A 股（6 位代码）与 美股（ticker，A 股默认基准 sh000300，美股默认 SPY）。"
    "【as_of 严格模式】当 as_of=True 时，仅返回预测截止时点已经完整收盘的数据，绝不包含任何 post-event 信息。"
    "传 prediction_cutoff_at 时，若截止时点早于事件日收盘，窗口严格截断至 T-1；否则最多截断至 T0。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "股票代码：A 股 6 位 / sh600519；美股 ticker AAPL / NVDA"},
            "event_date": {"type": "string", "description": "事件日 YYYYMMDD 或 YYYY-MM-DD"},
            "pre": {"type": "integer", "description": "事件前交易日数，默认20"},
            "post": {"type": "integer", "description": "事件后交易日数，默认20（as_of=True 时强制为 0）"},
            "index_symbol": {"type": "string", "description": "基准指数：A 股默认 sh000300(沪深300)；美股默认 SPY"},
            "as_of": {"type": "boolean", "description": "严格 as-of 模式：True=仅返回事件日及以前数据（禁止未来信息）"},
            "prediction_cutoff_at": {"type": "string", "description": "预测截止时间（ISO 8601，含时区）；用于排除截止时尚未收盘的交易日"},
        },
        "required": ["symbol", "event_date"],
    },
    internal=True,)
@cache.cached("event_study_cutoff_v2")
def event_study(symbol: str, event_date: str, pre: int = 20, post: int = 20,
                index_symbol: str = "", as_of: bool = False,
                prediction_cutoff_at: str = "") -> dict:
    try:
        try:
            ev = datetime.strptime(norm_date(event_date), "%Y%m%d").date()
        except ValueError:
            return err(f"无法识别事件日: {event_date}")
        pre = max(1, min(int(pre or 20), 60))
        # as_of 模式下强制 post=0；若显式 cutoff 在 T0 收盘前，后续还会截断到 T-1。
        if as_of:
            post = 0
        post = max(0, min(int(post or 0), 60))

        us = is_us_symbol(symbol)
        if us:
            sym = symbol.strip().upper()
            idx_sym = (index_symbol or "SPY").strip().upper()
        else:
            raw_symbol = (symbol or "").strip().lower()
            is_index_target = is_a_share_index_symbol(raw_symbol)
            sym = norm_index_symbol(raw_symbol) if is_index_target else norm_symbol(symbol)
            idx_sym = norm_index_symbol(index_symbol or "sh000300")

        # 向前多取 60 天缓冲保证 pre 窗口。严格 as-of 时从请求入口就截断到
        # prediction_cutoff_at 已经完整收盘的最后一天，而不是先取回未来行情
        # 再在输出层丢弃。没有显式 cutoff 的旧调用保持原来的 T0 截断语义。
        start8 = (ev - timedelta(days=pre * 2 + 60)).strftime("%Y%m%d")
        pre_event_cutoff = False
        completed_through = ev
        if as_of and prediction_cutoff_at:
            try:
                cutoff = datetime.fromisoformat(str(prediction_cutoff_at).replace("Z", "+00:00"))
            except ValueError:
                return err(f"无法识别预测截止时间: {prediction_cutoff_at}")
            market_close = cutoff.replace(
                hour=16 if us else 15, minute=0, second=0, microsecond=0,
            )
            completed_through = cutoff.date()
            if cutoff < market_close:
                completed_through -= timedelta(days=1)
            pre_event_cutoff = completed_through < ev
        end_day = min(ev, completed_through) if as_of else min(date.today(), ev + timedelta(days=post * 2 + 15))
        end8 = end_day.strftime("%Y%m%d")
        market = "US" if us else "CN"

        # 个股和基准没有依赖关系；并发取数将总等待降为两者中较慢的一个。
        # _fetch_close_payload 的 kline TTL 缓存会复用同日同基准结果。
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="event-study-price") as pool:
            stock_future = pool.submit(_fetch_close_payload, sym, start8, end8, market)
            index_future = pool.submit(_fetch_close_payload, idx_sym, start8, end8, market)
            stock_payload = stock_future.result()
            index_payload = index_future.result()
        stock_df, src_stock, attempts_stock = _payload_to_close(stock_payload, sym)
        idx_df, src_idx, attempts_idx = _payload_to_close(index_payload, idx_sym)
        idx_df = idx_df.rename(columns={"close": "idx_close"})

        if len(stock_df) == 0:
            return err(f"{sym} 无行情数据")
        idx_df = idx_df[["date", "close"]].rename(columns={"close": "idx_close"}) if "idx_close" not in idx_df.columns else idx_df

        df = pd.merge(stock_df, idx_df, on="date", how="inner").sort_values("date").reset_index(drop=True)
        if len(df) < pre + 3:
            return err(f"对齐后交易日不足（{len(df)} 天），无法构造 [-{pre},+{post}] 窗口")
        df["r_stock"] = df["close"].pct_change() * 100.0
        df["r_index"] = df["idx_close"].pct_change() * 100.0
        df["ar"] = df["r_stock"] - df["r_index"]

        ev_iso = ev.isoformat()
        if pre_event_cutoff:
            eligible = df.index[df["date"] <= end_day.isoformat()].tolist()
            if not eligible:
                return err(f"预测截止时间 {prediction_cutoff_at} 之前无完整交易日数据")
            anchor = eligible[-1]
            actual_event_day = ev_iso
            i_start = anchor - pre + 1
            i_end = anchor
            t_values = range(-pre, 0)
        else:
            ge = df.index[df["date"] >= ev_iso].tolist()
            if not ge:
                return err(f"事件日 {ev_iso} 之后无交易日数据")
            anchor = ge[0]
            actual_event_day = df.loc[anchor, "date"]
            i_start = anchor - pre
            i_end = min(anchor + post, len(df) - 1)
            t_values = range(-pre, -pre + (i_end - i_start + 1))
        if i_start < 1:  # 需要 i_start-1 计算首日收益
            return err(f"事件日前可用交易日不足 {pre} 天（仅 {anchor} 天）")
        win = df.loc[i_start:i_end].copy()
        win["car"] = win["ar"].cumsum()
        win["t"] = t_values

        def _cum_ret(days: pd.Series) -> Optional[float]:
            days = days.dropna()
            if len(days) == 0:
                return None
            return round(float(((1 + days / 100.0).prod() - 1) * 100.0), 4)

        def _endpoint_car(n: int) -> Optional[float]:
            """端点法 [T0, T0+N] CAR（%），与 Oracle labeler 的 car_tN 同口径。
            car = (p_asset_tN / p_asset_t0 - 1) - (p_bm_tN / p_bm_t0 - 1)
            """
            idx_n = anchor + n
            if idx_n >= len(df):
                return None
            p0 = float(df.loc[anchor, "close"])
            pN = float(df.loc[idx_n, "close"])
            bm_p0 = float(df.loc[anchor, "idx_close"])
            bm_pN = float(df.loc[idx_n, "idx_close"])
            if not p0 or not bm_p0 or not math.isfinite(pN) or not math.isfinite(bm_pN):
                return None
            return round(((pN / p0 - 1.0) - (bm_pN / bm_p0 - 1.0)) * 100.0, 4)

        rows = []
        for _, r in win.iterrows():
            rows.append({
                "t": int(r["t"]),
                "date": r["date"],
                "close": round(float(r["close"]), 3),
                "r_stock": None if pd.isna(r["r_stock"]) else round(float(r["r_stock"]), 4),
                "r_index": None if pd.isna(r["r_index"]) else round(float(r["r_index"]), 4),
                "ar": None if pd.isna(r["ar"]) else round(float(r["ar"]), 4),
                "car": None if pd.isna(r["car"]) else round(float(r["car"]), 4),
            })
        day0_rows = None if pre_event_cutoff else df.loc[anchor]
        # as_of 模式下禁止计算 post-event 指标；显式 pre-open cutoff 还会排除 T0。
        if as_of:
            summary = {
                "symbol": sym,
                "index_symbol": idx_sym,
                "market": "美股" if us else "A股",
                "event_date_requested": ev_iso,
                "event_day": actual_event_day,
                "event_day_is_trading_day": None if pre_event_cutoff else actual_event_day == ev_iso,
                "prediction_cutoff_at": prediction_cutoff_at or None,
                "data_cutoff_date": str(df.loc[anchor, "date"]),
                "event_day_data_included": not pre_event_cutoff,
                "window": (f"[-{pre}, -1] 交易日（strict pre-open as-of：T0 尚未收盘，已排除）"
                           if pre_event_cutoff else
                           f"[-{pre}, 0] 交易日（strict as-of：已截断 post-event 数据）"),
                "event_day_change_pct": (None if day0_rows is None or pd.isna(day0_rows["r_stock"])
                                         else round(float(day0_rows["r_stock"]), 4)),
                "event_day_idx_change_pct": (None if day0_rows is None or pd.isna(day0_rows["r_index"])
                                             else round(float(day0_rows["r_index"]), 4)),
                "event_day_ar_pct": (None if day0_rows is None or pd.isna(day0_rows["r_stock"]) or pd.isna(day0_rows["r_index"])
                                     else round(float(day0_rows["r_stock"]) - float(day0_rows["r_index"]), 4)),
                "pre5_cum_return_pct": _cum_ret(df.loc[max(anchor - 5, i_start):anchor - 1, "r_stock"])
                if not pre_event_cutoff and anchor - 1 >= i_start else _cum_ret(df.loc[max(anchor - 4, i_start):anchor, "r_stock"]),
                "pre20_cum_return_pct": _cum_ret(df.loc[i_start:anchor - 1, "r_stock"])
                if not pre_event_cutoff and anchor - 1 >= i_start else _cum_ret(df.loc[i_start:anchor, "r_stock"]),
                "pre5_cum_ar_pct": _cum_ret(df.loc[max(anchor - 5, i_start):anchor - 1, "ar"])
                if not pre_event_cutoff and anchor - 1 >= i_start else _cum_ret(df.loc[max(anchor - 4, i_start):anchor, "ar"]),
                # as_of 模式下显式声明无 post-event 数据
                "postN_as_of_blocked": True,
                "post1_car_endpoint_pct": None,
                "post3_car_endpoint_pct": None,
                "post5_car_endpoint_pct": None,
                "post5_cum_return_pct": None,
                "car_final_pct": rows[-1]["car"] if rows else None,  # 仅 cutoff 前窗口
                "note": "【STRICT AS-OF 模式】只返回预测截止时点已经完整收盘的数据。"
                        "若 prediction_cutoff_at 早于 T0 收盘，则 T0 行情字段为 null、窗口止于 T-1。"
                        "r_stock/r_index/ar/car 单位均为 %。可参考 pre5/pre20 漂移与 pre5 超额，"
                        "禁止使用/推断任何 post-event 指标。",
            }
        else:
            summary = {
                "symbol": sym,
                "index_symbol": idx_sym,
                "market": "美股" if us else "A股",
                "event_date_requested": ev_iso,
                "event_day": actual_event_day,
                "event_day_is_trading_day": actual_event_day == ev_iso,
                "window": f"[-{pre}, +{i_end - anchor}] 交易日",
                "event_day_change_pct": (None if pd.isna(day0_rows["r_stock"])
                                         else round(float(day0_rows["r_stock"]), 4)),
                "pre5_cum_return_pct": _cum_ret(df.loc[max(anchor - 5, i_start):anchor - 1, "r_stock"])
                if anchor - 1 >= i_start else None,
                "post5_cum_return_pct": _cum_ret(df.loc[anchor + 1:min(anchor + 5, i_end), "r_stock"]),
                "car_final_pct": rows[-1]["car"] if rows else None,
                "post1_car_endpoint_pct": _endpoint_car(1),
                "post3_car_endpoint_pct": _endpoint_car(3),
                "post5_car_endpoint_pct": _endpoint_car(5),
                "note": "r_stock/r_index/ar/car 单位均为 %。"
                        "postN_car_endpoint_pct = 端点法 [T0, T0+N] 超额收益（与 Oracle labeler 的 car_tN 同口径），"
                        "判断方向时优先看 post3_car_endpoint_pct；|post3_car_endpoint_pct| < 0.5 视为 neutral。"
                        "car_final_pct 是 [-pre,+post] 窗口累加法 CAR，含事件前漂移，仅供参考。",
            }
        line = {
            "kind": "line",
            "title": f"{sym.upper()} 事件研究 CAR 曲线（T0={actual_event_day}）",
            "payload": {
                "x": [str(r["t"]) for r in rows],
                "series": [{"name": "CAR(%)", "data": [r["car"] for r in rows]},
                           {"name": "AR(%)", "data": [r["ar"] for r in rows]}],
                "yname": "%",
                "event_date": actual_event_day,
            },
        }
        table = {
            "kind": "table",
            "title": f"事件窗口明细（{actual_event_day} 前后 {pre}/{max(0, i_end - anchor)} 日）",
            "payload": {
                "columns": ["t", "date", "close", "r_stock", "r_index", "ar", "car"],
                "rows": [[r[c] for c in ("t", "date", "close", "r_stock", "r_index", "ar", "car")]
                         for r in rows],
                "note": "收益单位 %；t=0 为事件日",
            },
        }
        result_meta = meta(f"{src_stock} + {src_idx}", len(rows))
        result_meta.update({
            "asset_provider": src_stock,
            "benchmark_provider": src_idx,
            "asset_attempts": attempts_stock,
            "benchmark_attempts": attempts_idx,
            "requested_start_date": start8,
            "requested_end_date": end8,
            "prediction_cutoff_at": prediction_cutoff_at or None,
            "data_cutoff_date": str(df.loc[anchor, "date"]),
        })
        return ok(
            {"summary": summary, "window": rows},
            result_meta,
            artifacts=[line, table],
        )
    except Exception as e:  # noqa: BLE001
        return err(f"事件研究失败: {type(e).__name__}: {e}")
