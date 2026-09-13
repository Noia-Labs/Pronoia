"""Analysis skills: event_study — 事件研究法 (design.md §5).

取事件日前后 [-pre, +post] 交易日的个股日K与指数日K（向前多取缓冲保证窗口），
计算日收益 r_stock / r_index，AR_t = r_stock - r_index，CAR_t = ΣAR（自 -pre 起累计）。
所有收益类字段单位：%（百分比）。

支持 A 股（6 位代码 + 沪深 300 等指数）与 美股（ticker + SPY/QQQ 等 ETF）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional
import math

import akshare as ak
import pandas as pd

from ..market_runtime import call_sina_history
from .market import (
    _clean_ohlcv,
    is_a_share_index_symbol,
    is_us_symbol,
    norm_date,
    norm_index_symbol,
    norm_symbol,
)
from .registry import err, meta, ok, skill
from . import cache


def _fetch_stock_close(sym: str, start8: str, end8: str) -> tuple[pd.DataFrame, str]:
    try:
        df = call_sina_history(ak.stock_zh_a_daily, symbol=sym, start_date=start8, end_date=end8, adjust="qfq")
        if df is None or len(df) == 0:
            raise ValueError("sina 日K为空")
        src = "akshare.stock_zh_a_daily"
    except Exception as e1:  # noqa: BLE001
        try:
            df = ak.stock_zh_a_hist_tx(symbol=sym, start_date=start8, end_date=end8, adjust="qfq")
            src = "akshare.stock_zh_a_hist_tx"
        except Exception as e2:  # noqa: BLE001
            raise ValueError(
                f"{sym} 行情获取失败（可能代码不存在）: sina({type(e1).__name__}), tx({type(e2).__name__})"
            )
    if df is None or len(df) == 0:
        raise ValueError(f"{sym} 在 {start8}~{end8} 无行情数据（可能代码不存在）")
    df, _ = _clean_ohlcv(df, limit=100000)
    return df[["date", "close"]], src


def _fetch_a_share_index_close(sym: str, start8: str) -> tuple[pd.DataFrame, str]:
    """A 股指数日K。返回 [date, close] + 数据源。"""
    try:
        df = call_sina_history(ak.stock_zh_index_daily, symbol=sym)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"指数 {sym} 获取失败: {type(e).__name__}: {e}")
    if df is None or len(df) == 0:
        raise ValueError(f"指数 {sym} 无数据")
    df, _ = _clean_ohlcv(df, limit=100000)
    df = df[df["date"] >= f"{start8[:4]}-{start8[4:6]}-{start8[6:]}"]
    if len(df) == 0:
        raise ValueError(f"指数 {sym} 在 {start8} 之后无数据")
    return df[["date", "close"]], "akshare.stock_zh_index_daily"


def _fetch_us_close(sym: str) -> tuple[pd.DataFrame, str]:
    """美股日K（akshare.stock_us_daily）。返回 [date, close] + 数据源。"""
    try:
        df = call_sina_history(ak.stock_us_daily, symbol=sym, adjust="qfq")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"美股 {sym} 日K获取失败: {type(e).__name__}: {e}")
    if df is None or len(df) == 0:
        raise ValueError(f"美股 {sym} 无日K数据")
    df, _ = _clean_ohlcv(df, limit=100000)
    return df[["date", "close"]], "akshare.stock_us_daily"


def _fetch_us_index_close(sym: str) -> tuple[pd.DataFrame, str]:
    """美股指数 / ETF 当作「美股」用 stock_us_daily 取。返回 [date, close]。"""
    return _fetch_us_close(sym)


@skill(
    "event_study",
    "事件研究法：以事件日为 T0，计算窗口 [-pre,+post] 内个股相对指数的超额收益 AR 与累计超额收益 CAR，"
    "并给出事件日前5日/后5日累计收益、CAR终值、事件日涨跌幅。收益单位%。"
    "支持 A 股（6 位代码）与 美股（ticker，A 股默认基准 sh000300，美股默认 SPY）。"
    "【as_of 严格模式】当 as_of=True 时，仅返回信息可得日前一交易日收盘及之前的数据，绝不包含任何 post-event 信息"
    "（禁止计算/返回事件日涨跌或 postN_car_endpoint_pct / post5_cum_return 等未来指标）。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "股票代码：A 股 6 位 / sh600519；美股 ticker AAPL / NVDA"},
            "event_date": {"type": "string", "description": "事件日 YYYYMMDD 或 YYYY-MM-DD"},
            "pre": {"type": "integer", "description": "事件前交易日数，默认20"},
            "post": {"type": "integer", "description": "事件后交易日数，默认20（as_of=True 时强制为 0）"},
            "index_symbol": {"type": "string", "description": "基准指数：A 股默认 sh000300(沪深300)；美股默认 SPY"},
            "as_of": {"type": "boolean", "description": "严格 as-of 模式：True=仅返回事件日及以前数据（禁止未来信息）"},
        },
        "required": ["symbol", "event_date"],
    },
    internal=True,)
@cache.cached("event_study")
def event_study(symbol: str, event_date: str, pre: int = 20, post: int = 20,
                index_symbol: str = "", as_of: bool = False) -> dict:
    try:
        try:
            ev = datetime.strptime(norm_date(event_date), "%Y%m%d").date()
        except ValueError:
            return err(f"无法识别事件日: {event_date}")
        pre = max(1, min(int(pre or 20), 60))
        # as_of 模式下强制 post=0；下方还会把可见行情严格
        # 截断在事件日前一交易日收盘，避免盘前/盘中事件看到 T0 收盘。
        if as_of:
            post = 0
        post = max(0, min(int(post or 0), 60))

        us = is_us_symbol(symbol)
        if us:
            sym = symbol.strip().upper()
            idx_sym = (index_symbol or "SPY").strip().upper()
            stock_df, src_stock = _fetch_us_close(sym)
            idx_df, src_idx = _fetch_us_index_close(idx_sym)
        else:
            raw_symbol = (symbol or "").strip().lower()
            is_index_target = is_a_share_index_symbol(raw_symbol)
            sym = norm_index_symbol(raw_symbol) if is_index_target else norm_symbol(symbol)
            idx_sym = norm_index_symbol(index_symbol or "sh000300")
            # 向前多取 60 天缓冲保证 pre 窗口；向后取到 min(今天, 事件+缓冲)
            start8 = (ev - timedelta(days=pre * 2 + 60)).strftime("%Y%m%d")
            end8 = min(date.today(), ev + timedelta(days=post * 2 + 15)).strftime("%Y%m%d")
            if is_index_target:
                stock_df, src_stock = _fetch_a_share_index_close(sym, start8)
            else:
                stock_df, src_stock = _fetch_stock_close(sym, start8, end8)
            try:
                idx_raw = call_sina_history(ak.stock_zh_index_daily, symbol=idx_sym)
            except Exception as e:  # noqa: BLE001
                return err(f"基准指数 {idx_sym} 获取失败: {type(e).__name__}: {e}")
            if idx_raw is None or len(idx_raw) == 0:
                return err(f"基准指数 {idx_sym} 无数据")
            idx_df, _ = _clean_ohlcv(idx_raw, limit=100000)
            src_idx = "akshare.stock_zh_index_daily"
            idx_df = idx_df[["date", "close"]].rename(columns={"close": "idx_close"})
            idx_df = idx_df[(idx_df["date"] >= f"{start8[:4]}-{start8[4:6]}-{start8[6:]}")]

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
        ge = df.index[df["date"] >= ev_iso].tolist()
        if not ge:
            return err(f"事件日 {ev_iso} 之后无交易日数据")
        t0 = ge[0]
        actual_event_day = df.loc[t0, "date"]
        i_start = t0 - pre
        if i_start < 1:  # 需要 i_start-1 计算首日收益
            return err(f"事件日前可用交易日不足 {pre} 天（仅 {t0} 天）")
        i_end = (t0 - 1) if as_of else min(t0 + post, len(df) - 1)
        win = df.loc[i_start:i_end].copy()
        win["car"] = win["ar"].cumsum()
        win["t"] = range(-pre, -pre + len(win))

        def _cum_ret(days: pd.Series) -> Optional[float]:
            days = days.dropna()
            if len(days) == 0:
                return None
            return round(float(((1 + days / 100.0).prod() - 1) * 100.0), 4)

        def _endpoint_car(n: int) -> Optional[float]:
            """端点法 [T0, T0+N] CAR（%），与 Oracle labeler 的 car_tN 同口径。
            car = (p_asset_tN / p_asset_t0 - 1) - (p_bm_tN / p_bm_t0 - 1)
            """
            idx_n = t0 + n
            if idx_n >= len(df):
                return None
            p0 = float(df.loc[t0, "close"])
            pN = float(df.loc[idx_n, "close"])
            bm_p0 = float(df.loc[t0, "idx_close"])
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
        day0_rows = df.loc[t0]
        # The tool receives a date, not a reliable intraday timestamp. The only
        # universally safe cutoff is the previous trading close. This is
        # conservative for post-close announcements but never leaks T0 close
        # into a pre-open/intraday decision.
        if as_of:
            prior_close_day = str(df.loc[t0 - 1, "date"])
            summary = {
                "symbol": sym,
                "index_symbol": idx_sym,
                "market": "美股" if us else "A股",
                "event_date_requested": ev_iso,
                "event_day": actual_event_day,
                "event_day_is_trading_day": actual_event_day == ev_iso,
                "window": f"[-{pre}, -1] 交易日（strict as-of；截止前一交易日收盘 {prior_close_day}）",
                "information_cutoff": "previous_trading_close",
                "information_cutoff_date": prior_close_day,
                "event_day_change_pct": None,
                "event_day_idx_change_pct": None,
                "event_day_ar_pct": None,
                "pre5_cum_return_pct": _cum_ret(df.loc[max(t0 - 5, i_start):t0 - 1, "r_stock"])
                if t0 - 1 >= i_start else None,
                "pre20_cum_return_pct": _cum_ret(df.loc[i_start:t0 - 1, "r_stock"])
                if t0 - 1 >= i_start else None,
                "pre5_cum_ar_pct": _cum_ret(df.loc[max(t0 - 5, i_start):t0 - 1, "ar"])
                if t0 - 1 >= i_start else None,
                # as_of 模式下显式声明无 post-event 数据
                "postN_as_of_blocked": True,
                "post1_car_endpoint_pct": None,
                "post3_car_endpoint_pct": None,
                "post5_car_endpoint_pct": None,
                "post5_cum_return_pct": None,
                "car_final_pct": rows[-1]["car"] if rows else None,  # 仅 [-pre, -1]
                "note": "【STRICT AS-OF 模式】仅返回事件日前一交易日收盘及更早的数据；"
                        "T0 当日涨跌与所有 T+N 字段均不可见。r_stock/r_index/ar/car 单位均为 %。"
                        "可参考 pre5/pre20 漂移与 pre5 超额，禁止使用/推断事件日或 post-event 指标。",
            }
        else:
            summary = {
                "symbol": sym,
                "index_symbol": idx_sym,
                "market": "美股" if us else "A股",
                "event_date_requested": ev_iso,
                "event_day": actual_event_day,
                "event_day_is_trading_day": actual_event_day == ev_iso,
                "window": f"[-{pre}, +{i_end - t0}] 交易日",
                "event_day_change_pct": (None if pd.isna(day0_rows["r_stock"])
                                         else round(float(day0_rows["r_stock"]), 4)),
                "pre5_cum_return_pct": _cum_ret(df.loc[max(t0 - 5, i_start):t0 - 1, "r_stock"])
                if t0 - 1 >= i_start else None,
                "post5_cum_return_pct": _cum_ret(df.loc[t0 + 1:min(t0 + 5, i_end), "r_stock"]),
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
            "title": (
                f"事件前可见窗口明细（截止 {df.loc[t0 - 1, 'date']}）"
                if as_of else f"事件窗口明细（{actual_event_day} 前后 {pre}/{i_end - t0} 日）"
            ),
            "payload": {
                "columns": ["t", "date", "close", "r_stock", "r_index", "ar", "car"],
                "rows": [[r[c] for c in ("t", "date", "close", "r_stock", "r_index", "ar", "car")]
                         for r in rows],
                "note": (
                    "收益单位 %；严格 as-of 已截断至 t=-1，不包含事件日收盘"
                    if as_of else "收益单位 %；t=0 为事件日"
                ),
            },
        }
        return ok(
            {"summary": summary, "window": rows},
            meta(f"{src_stock} + {src_idx}", len(rows)),
            artifacts=[line, table],
        )
    except Exception as e:  # noqa: BLE001
        return err(f"事件研究失败: {type(e).__name__}: {e}")
