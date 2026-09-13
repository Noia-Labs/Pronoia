from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

from ..market_runtime import call_sina_history
from .benchmark import ABSOLUTE_RETURN_BENCHMARK, resolve_event_benchmark

try:
    import akshare as ak  # type: ignore
except Exception:
    ak = None  # optional for US-only run


SECTOR_ETF_CN = {
    "技术硬件与设备": "512480",  # 半导体ETF (国证)
    "半导体与半导体生产设备": "512480",
    "资本货物": "512580",  # 环保ETF，不精确；资本货物暂无单一指数，用 HS300 兜底
    "汽车与汽车零部件": "516110",  # 汽车ETF
    "材料": "512400",  # 有色金属ETF
    "食品饮料与烟草": "512690",  # 酒ETF
    "食品、饮料与烟草": "512690",
    "媒体": "512980",  # 传媒ETF
    "运输": "512760",  # 芯片ETF兜底；运输用515790 光伏/电新？不对，保持 510300 兜底
    "商业和专业服务": "510300",
    "银行": "512800",  # 银行ETF
    "多元金融": "512800",
    "保险": "512800",
    "软件与服务": "515030",  # 新能源车ETF？软件应是 515230 软件ETF
    "制药、生物科技与生命科学": "512290",  # 生物医药ETF
    "医疗保健设备与服务": "512290",
    "能源": "159945",  # 能源ETF（广发深证）
    "公用事业": "159945",
    "房地产": "512200",  # 房地产ETF
    "消费服务": "159928",  # 消费ETF
    "零售业": "159928",
    "耐用消费品与服装": "159928",
    "家庭与个人用品": "159928",
    "电信服务": "515050",  # 5G ETF
    "技术硬件设备": "512480",
}

BENCHMARK_CN_DEFAULT = "sh000300"
BENCHMARK_US_DEFAULT = "SPY"

DATE_FMTS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d", "%Y/%m/%d")

SUPPORTED_ORACLE_MARKETS = frozenset({"CN", "US", "HK", "FUTURES"})
UNSUPPORTED_ORACLE_MARKETS = frozenset({"CRYPTO", "FX"})


class UnsupportedOracleMarketError(ValueError):
    """Raised when an event requests a market without a verified price adapter."""


def _normalize_market(market: str) -> str:
    """Return the canonical UI market name, or fail before any provider call.

    Crypto and FX are deliberately rejected until a historical daily adapter is
    configured.  Falling through to the US equity endpoint would be worse than a
    hard failure because it can silently attach prices for an unrelated ticker.
    """
    value = str(market or "").strip().upper()
    aliases = {
        "A_SHARE": "CN", "A-SHARE": "CN", "CHINA": "CN",
        "USA": "US", "NASDAQ": "US", "NYSE": "US",
        "HONGKONG": "HK", "HONG_KONG": "HK",
        "FUTURE": "FUTURES", "FUT": "FUTURES",
    }
    value = aliases.get(value, value)
    if value in SUPPORTED_ORACLE_MARKETS:
        return value
    if value in UNSUPPORTED_ORACLE_MARKETS:
        raise UnsupportedOracleMarketError(
            f"Oracle 暂不支持 {value} 历史行情；当前仅支持 CN/US/HK/FUTURES。"
            "请接入并校验专用行情源后再生成标签，系统不会用美股或模拟价格代替。"
        )
    raise UnsupportedOracleMarketError(
        f"未知市场 {value or '<empty>'}；Oracle 当前仅支持 CN/US/HK/FUTURES，"
        "且不会猜测行情路由。"
    )


# -------------------- AKSHARE (CN 行情) --------------------
def _ak_cn_hist(symbol: str, start_date: str, end_date: str, *, retries: int = 4, sleep_s: float = 0.9):
    """
    CN 行情：akshare.stock_zh_a_daily (Sina 源，不走 push2his)
    symbol 为 6 位 A 股代码。自动补 sh/sz 前缀（6/9/5 开头 → sh；0/3/1/2 → sz）
    返回 Series: index=date, value=close
    重试 4 次 + sleep 0.9s
    """
    if ak is None:
        return None
    code = str(symbol or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        return None
    first = code[0]
    if first in {"6", "9", "5"}:
        prefixed = f"sh{code}"
    elif first in {"0", "3", "1", "2"}:
        prefixed = f"sz{code}"
    else:
        prefixed = f"sh{code}"
    sd_d = dt.date.fromisoformat(start_date)
    ed_d = dt.date.fromisoformat(end_date)
    last_err = None
    for attempt in range(1, int(retries) + 1):
        try:
            time.sleep(sleep_s + 0.4 * (attempt - 1))
            # 主方案：stock_zh_a_daily (Sina, 前复权) —— 与 event_study_skill 对齐
            df = call_sina_history(ak.stock_zh_a_daily, symbol=prefixed, adjust="qfq")
            ok_df = df is not None and len(df) > 0 and "date" in df.columns and "close" in df.columns
            # 备用：stock_zh_a_hist_tx (Tencent)
            if not ok_df and hasattr(ak, "stock_zh_a_hist_tx"):
                try:
                    sd_s = start_date.replace("-", "")
                    ed_s = end_date.replace("-", "")
                    df = ak.stock_zh_a_hist_tx(symbol=prefixed, start_date=sd_s, end_date=ed_s, adjust="qfq")
                    ok_df = df is not None and len(df) > 0 and "date" in df.columns and "close" in df.columns
                except Exception as e2:
                    last_err = e2
            if not ok_df:
                last_err = ValueError(f"empty cn resp len={0 if df is None else len(df)}")
                continue
            df["date"] = pd.to_datetime(df["date"]).dt.date
            s = df.set_index("date")["close"].sort_index().astype(float)
            s = s[~s.index.duplicated()]
            s = s[(s.index >= sd_d) & (s.index <= ed_d)]
            time.sleep(sleep_s * 0.5)
            return s
        except Exception as e:
            last_err = e
            msg = str(e)
            if "ProxyError" in msg or "RemoteDisconnected" in msg or "Connection aborted" in msg or "429" in msg or "Too Many" in msg:
                time.sleep(sleep_s * (1 + attempt))
                continue
            if attempt >= int(retries):
                print(f"[WARN] ak cn {symbol} ({prefixed}) err (attempt={attempt}): {type(e).__name__}: {e}")
            time.sleep(sleep_s)
    if last_err is not None:
        print(f"[WARN] ak cn {symbol} all attempts fail: {type(last_err).__name__}: {last_err}")
    return None


def _ak_cn_index_hist(benchmark_code: str, start_date: str, end_date: str, *, retries: int = 4, sleep_s: float = 1.0):
    """
    CN 指数：ak.stock_zh_index_daily(symbol="sh000300")；
    CN ETF：优先 fund_etf_hist_em，失败 fallback stock_zh_a_hist (走个股接口也能拉 ETF)。
    返回 Series date→close
    """
    if ak is None:
        return None
    code = str(benchmark_code or "").strip().lower()
    if not code:
        return None
    sd = start_date
    ed = end_date
    last_err = None
    # 先拆分: sh/sz + 6 root
    prefix = None; root = None
    if len(code) == 8 and code[:2] in {"sh", "sz"} and code[2:].isdigit():
        prefix = code[:2]; root = code[2:]
    if len(code) == 6 and code.isdigit():
        root = code
        prefix = "sz" if code.startswith(("15", "399")) else "sh"

    # ---- ETF 判定 ----
    def _is_etf(r):
        return r is not None and r[:2] in {"51", "56", "58", "15"}

    # ---- ETF 路径：优先 akshare fund_etf_hist_em，再 fallback _ak_cn_hist ----
    if _is_etf(root):
        # fund_etf_hist_em: symbol = 6位纯数字, 如 "510300"
        for attempt in range(1, int(retries) + 1):
            try:
                time.sleep(sleep_s + 0.4 * (attempt - 1))
                df = ak.fund_etf_hist_em(symbol=root, period="daily",
                                         start_date=sd.replace("-",""), end_date=ed.replace("-",""),
                                         adjust="qfq")
                if df is not None and len(df) > 0 and "收盘" in df.columns and "日期" in df.columns:
                    df["date"] = pd.to_datetime(df["日期"]).dt.date
                    s = df.set_index("date")["收盘"].sort_index().astype(float)
                    sd_d = dt.date.fromisoformat(sd); ed_d = dt.date.fromisoformat(ed)
                    s = s[(s.index >= sd_d) & (s.index <= ed_d)]
                    if len(s) > 0:
                        time.sleep(sleep_s * 0.5)
                        return s
                # fund_etf_hist_em 空 → fallback stock_zh_index_daily
                if prefix and root:
                    try:
                        df2 = call_sina_history(ak.stock_zh_index_daily, symbol=f"{prefix}{root}")
                        if df2 is not None and len(df2) > 0 and "date" in df2.columns and "close" in df2.columns:
                            df2["date"] = pd.to_datetime(df2["date"]).dt.date
                            s2 = df2.set_index("date")["close"].sort_index().astype(float)
                            sd_d = dt.date.fromisoformat(sd); ed_d = dt.date.fromisoformat(ed)
                            s2 = s2[(s2.index >= sd_d) & (s2.index <= ed_d)]
                            if len(s2) > 0:
                                time.sleep(sleep_s * 0.5)
                                return s2
                    except Exception:
                        pass
                # 最后兜底走 _ak_cn_hist（个股接口，很多 ETF 也能查到）
                s3 = _ak_cn_hist(root, sd, ed, retries=1, sleep_s=0.2)
                if s3 is not None and len(s3) > 0:
                    return s3
                last_err = ValueError("etf all routes empty")
            except Exception as e:
                last_err = e
                msg = str(e)
                if "RemoteDisconnected" in msg or "Connection aborted" in msg or "429" in msg:
                    time.sleep(sleep_s * (1 + attempt)); continue
                if attempt >= int(retries):
                    print(f"[WARN] ak etf {code} err (attempt={attempt}): {type(e).__name__}: {e}")
                time.sleep(sleep_s)
        # 还是失败 → 再走一次 stock_zh_index_daily 兜底
        if prefix and root:
            s_idx = _ak_cn_index_hist_noetf(f"{prefix}{root}", sd, ed, retries=max(2, retries - 1), sleep_s=sleep_s)
            if s_idx is not None and len(s_idx) > 0:
                return s_idx
        if last_err is not None:
            print(f"[WARN] ak etf {code} all fail: {type(last_err).__name__}: {last_err}")
        return None

    # ---- 纯指数路径 (sh000300 / sz399006 / etc) ----
    if prefix and root:
        return _ak_cn_index_hist_noetf(f"{prefix}{root}", sd, ed, retries=retries, sleep_s=sleep_s)
    # 6 位纯数字兜底 → 当个股 ETF 走 _ak_cn_hist
    if len(code) == 6 and code.isdigit():
        return _ak_cn_hist(code, sd, ed, retries=retries, sleep_s=sleep_s)
    return None


def _ak_cn_index_hist_noetf(code: str, start_date: str, end_date: str, *, retries: int = 4, sleep_s: float = 1.0):
    """纯 index 接口：stock_zh_index_daily(symbol=sh000300 / sz399006)"""
    if ak is None:
        return None
    code = code.lower()
    sd, ed = start_date, end_date
    last_err = None
    if len(code) == 8 and code[:2] in {"sh", "sz"} and code[2:].isdigit():
        for attempt in range(1, int(retries) + 1):
            try:
                time.sleep(sleep_s + 0.4 * (attempt - 1))
                df = call_sina_history(ak.stock_zh_index_daily, symbol=code)
                if df is None or len(df) == 0 or "date" not in df.columns:
                    last_err = ValueError("empty index resp"); continue
                df["date"] = pd.to_datetime(df["date"]).dt.date
                s = df.set_index("date")["close"].sort_index().astype(float)
                sd_d = dt.date.fromisoformat(sd); ed_d = dt.date.fromisoformat(ed)
                s = s[(s.index >= sd_d) & (s.index <= ed_d)]
                time.sleep(sleep_s * 0.6)
                return s
            except Exception as e:
                last_err = e
                msg = str(e)
                if "RemoteDisconnected" in msg or "Connection aborted" in msg or "429" in msg:
                    time.sleep(sleep_s * (1 + attempt)); continue
                if attempt >= int(retries):
                    print(f"[WARN] ak index {code} err (attempt={attempt}): {type(e).__name__}: {e}")
                time.sleep(sleep_s)
        if last_err is not None:
            print(f"[WARN] ak index {code} all fail: {type(last_err).__name__}: {last_err}")
    return None


def _ak_us_hist(symbol: str, start_date: str, end_date: str, *, retries: int = 4, sleep_s: float = 0.9):
    """
    US 行情：akshare.stock_us_daily
    symbol 为纯代码 (SPY/QQQ/AAPL 等)。返回 Series index=date, value=close
    重试 4 次 + sleep 0.9s
    """
    if ak is None:
        return None
    code = str(symbol or "").strip().upper()
    if not code:
        return None
    sd_d = dt.date.fromisoformat(start_date)
    ed_d = dt.date.fromisoformat(end_date)
    last_err = None
    for attempt in range(1, int(retries) + 1):
        try:
            time.sleep(sleep_s + 0.5 * (attempt - 1))
            df = call_sina_history(ak.stock_us_daily, symbol=code, adjust="qfq")
            if df is None or len(df) == 0 or "date" not in df.columns or "close" not in df.columns:
                last_err = ValueError(f"empty us resp len={0 if df is None else len(df)}")
                continue
            df["date"] = pd.to_datetime(df["date"]).dt.date
            s = df.set_index("date")["close"].sort_index().astype(float)
            s = s[~s.index.duplicated()]
            s = s[(s.index >= sd_d) & (s.index <= ed_d)]
            time.sleep(sleep_s * 0.6)
            return s
        except Exception as e:
            last_err = e
            msg = str(e)
            if "ProxyError" in msg or "RemoteDisconnected" in msg or "Connection aborted" in msg or "429" in msg or "Too Many" in msg:
                time.sleep(sleep_s * (1 + attempt))
                continue
            if attempt >= int(retries):
                print(f"[WARN] ak us {symbol} err (attempt={attempt}): {type(e).__name__}: {e}")
            time.sleep(sleep_s)
    if last_err is not None:
        print(f"[WARN] ak us {symbol} all attempts fail: {type(last_err).__name__}: {last_err}")
    return None


def _close_series_from_frame(
    frame: pd.DataFrame,
    start_date: str,
    end_date: str,
    *,
    date_columns: tuple[str, ...] = ("date", "日期"),
    close_columns: tuple[str, ...] = ("close", "收盘", "收盘价", "latest"),
) -> Optional[pd.Series]:
    """Normalize an AkShare daily frame without assuming one provider schema."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    df = frame.copy()
    date_column = next((name for name in date_columns if name in df.columns), None)
    close_column = next((name for name in close_columns if name in df.columns), None)
    if close_column is None:
        return None
    raw_dates = df[date_column] if date_column is not None else pd.Series(df.index, index=df.index)
    parsed_dates = pd.to_datetime(raw_dates, errors="coerce")
    values = pd.to_numeric(df[close_column], errors="coerce")
    normalized = pd.DataFrame({
        "date": pd.Series(parsed_dates).reset_index(drop=True),
        "close": pd.Series(values).reset_index(drop=True),
    }).dropna(subset=["date", "close"])
    if normalized.empty:
        return None
    normalized["date"] = normalized["date"].dt.date
    sd_d = dt.date.fromisoformat(start_date)
    ed_d = dt.date.fromisoformat(end_date)
    normalized = normalized[
        (normalized["date"] >= sd_d) & (normalized["date"] <= ed_d)
    ]
    if normalized.empty:
        return None
    series = normalized.set_index("date")["close"].sort_index().astype(float)
    series = series[~series.index.duplicated(keep="last")]
    series = series[np.isfinite(series.to_numpy(dtype=float))]
    return series if len(series) else None


def _normalize_hk_symbol(symbol: str) -> str:
    """Normalize common HK equity spellings to AkShare's five-digit code."""
    value = str(symbol or "").strip().upper()
    value = re.sub(r"^(?:HK[.:]?)", "", value)
    value = re.sub(r"(?:[.]HK)$", "", value)
    if not value.isdigit() or len(value) > 5:
        raise ValueError(f"无效港股代码 {symbol!r}；请使用 5 位代码，例如 00700")
    return value.zfill(5)


def _normalize_futures_symbol(symbol: str) -> str:
    """Normalize a domestic futures contract/main-continuous symbol."""
    value = str(symbol or "").strip().upper().replace(" ", "")
    if not re.fullmatch(r"[A-Z]{1,4}[0-9]{1,4}", value):
        raise ValueError(
            f"无效期货代码 {symbol!r}；请使用 AkShare/Sina 合约格式，例如 RB0、IF0、CU2501"
        )
    return value


def _ak_hk_hist(symbol: str, start_date: str, end_date: str, *, retries: int = 3, sleep_s: float = 0.9):
    """HK equity daily close via AkShare, with an independent Sina fallback."""
    if ak is None:
        return None
    code = _normalize_hk_symbol(symbol)
    start_compact = start_date.replace("-", "")
    end_compact = end_date.replace("-", "")
    last_err: Optional[Exception] = None
    for attempt in range(1, int(retries) + 1):
        time.sleep(sleep_s + 0.4 * (attempt - 1))
        if hasattr(ak, "stock_hk_hist"):
            try:
                df = ak.stock_hk_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_compact,
                    end_date=end_compact,
                    adjust="qfq",
                )
                series = _close_series_from_frame(df, start_date, end_date)
                if series is not None:
                    return series
            except Exception as exc:
                last_err = exc
        if hasattr(ak, "stock_hk_daily"):
            try:
                df = call_sina_history(ak.stock_hk_daily, symbol=code, adjust="qfq")
                series = _close_series_from_frame(df, start_date, end_date)
                if series is not None:
                    return series
            except Exception as exc:
                last_err = exc
        if last_err is None:
            last_err = ValueError("港股主数据源与备用源均为空")
        if attempt < int(retries):
            time.sleep(sleep_s * attempt)
    if last_err is not None:
        print(f"[WARN] ak hk {code} all attempts fail: {type(last_err).__name__}: {last_err}")
    return None


def _ak_hk_index_hist(symbol: str, start_date: str, end_date: str, *, retries: int = 3, sleep_s: float = 0.9):
    """HK index daily close (for example HSI/HSTECH) via verified index APIs."""
    if ak is None:
        return None
    code = str(symbol or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{2,16}", code):
        raise ValueError(f"无效港股指数代码 {symbol!r}")
    last_err: Optional[Exception] = None
    for attempt in range(1, int(retries) + 1):
        time.sleep(sleep_s + 0.4 * (attempt - 1))
        if hasattr(ak, "stock_hk_index_daily_sina"):
            try:
                df = ak.stock_hk_index_daily_sina(symbol=code)
                series = _close_series_from_frame(df, start_date, end_date)
                if series is not None:
                    return series
            except Exception as exc:
                last_err = exc
        if hasattr(ak, "stock_hk_index_daily_em"):
            try:
                df = ak.stock_hk_index_daily_em(symbol=code)
                series = _close_series_from_frame(df, start_date, end_date)
                if series is not None:
                    return series
            except Exception as exc:
                last_err = exc
        if last_err is None:
            last_err = ValueError("港股指数主数据源与备用源均为空")
        if attempt < int(retries):
            time.sleep(sleep_s * attempt)
    if last_err is not None:
        print(f"[WARN] ak hk-index {code} all attempts fail: {type(last_err).__name__}: {last_err}")
    return None


def _ak_futures_hist(symbol: str, start_date: str, end_date: str, *, retries: int = 3, sleep_s: float = 0.9):
    """Domestic futures daily close, preferring the main-continuous API."""
    if ak is None:
        return None
    code = _normalize_futures_symbol(symbol)
    start_compact = start_date.replace("-", "")
    end_compact = end_date.replace("-", "")
    last_err: Optional[Exception] = None
    for attempt in range(1, int(retries) + 1):
        time.sleep(sleep_s + 0.4 * (attempt - 1))
        if hasattr(ak, "futures_main_sina"):
            try:
                df = ak.futures_main_sina(
                    symbol=code,
                    start_date=start_compact,
                    end_date=end_compact,
                )
                series = _close_series_from_frame(df, start_date, end_date)
                if series is not None:
                    return series
            except Exception as exc:
                last_err = exc
        if hasattr(ak, "futures_zh_daily_sina"):
            try:
                df = ak.futures_zh_daily_sina(symbol=code)
                series = _close_series_from_frame(df, start_date, end_date)
                if series is not None:
                    return series
            except Exception as exc:
                last_err = exc
        if last_err is None:
            last_err = ValueError("期货主力连续与品种日线数据源均为空")
        if attempt < int(retries):
            time.sleep(sleep_s * attempt)
    if last_err is not None:
        print(f"[WARN] ak futures {code} all attempts fail: {type(last_err).__name__}: {last_err}")
    return None


MARKET_TIMEZONES = {
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "FUTURES": "Asia/Shanghai",
    "US": "America/New_York",
}


def _event_datetime_in_market(s, market: str = "") -> Optional[dt.datetime]:
    if isinstance(s, dt.datetime):
        parsed = s
    else:
        value = str(s or "").strip()
        if not value:
            return None
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            parsed = None
            for fmt in DATE_FMTS:
                try:
                    parsed = dt.datetime.strptime(value, fmt)
                    break
                except Exception:
                    continue
            if parsed is None:
                return None
    if parsed.tzinfo is not None:
        timezone_name = MARKET_TIMEZONES.get(str(market or "").strip().upper())
        if timezone_name:
            parsed = parsed.astimezone(ZoneInfo(timezone_name))
    return parsed


def _parse_date(s, market: str = "") -> Optional[dt.date]:
    if s is None or (isinstance(s, float) and math.isnan(s)):
        return None
    if isinstance(s, dt.date) and not isinstance(s, dt.datetime):
        return s
    parsed = _event_datetime_in_market(s, market)
    return parsed.date() if parsed is not None else None


def _yf_ticker_for(symbol: str, market: str, benchmark: Optional[str]) -> str:
    """
    为 labeller 内部的「分类 + akshare 下载」统一返回 canonical ticker 格式：
      - CN 个股（60/68/000/002/300/920 等 6 位数字前缀）→ 保留 .SS/.SZ 后缀（走 Phase2 个股接口）
      - CN 指数 / ETF → 一律 小写 sh/sz + 6 位数字（走 Phase3 CN-index 接口），包括：
          * 输入本身带 SH/SZ 字母前缀
          * 6 位 ETF 数字前缀（51/56/58/15 开头）
          * 6 位指数代码（000/399/88 开头）
      - US → 原样大写
      - HK → 5 位股票代码 + .HK；指数保留 HSI/HSTECH 等代码
      - FUTURES → AkShare/Sina 合约代码（RB0/IF0/CU2501）
    """
    market = _normalize_market(market)
    symbol = str(symbol or "").strip()
    if market == "CN":
        # 先剥字母前缀：SH510300 / sz399006 → (prefix, 6-digit root)
        s_low = symbol.lower()
        if s_low[:2] in {"sh", "sz"} and len(s_low) >= 8 and s_low[2:8].isdigit():
            return s_low[:8]  # sh510300 / sz399006
        if len(symbol) == 6 and symbol.isdigit():
            root = symbol
            # ---- 上证/深证 指数白名单 ----
            CN_INDEX_CODES = {
                "000001", "000016", "000300", "000905", "000852", "000688", "000010", "000009",
                "000015", "000017", "000018", "000019", "000020", "000021", "000022",
                "000023", "000024", "000025", "000026", "000027", "000028", "000029",
                "000030", "000031", "000032", "000033", "000034", "000035", "000036",
                "000037", "000038", "000039", "000040", "000041", "000042", "000043",
                "000044", "000045", "000046", "000047", "000048", "000049", "000050",
                "880001", "880472", "880813", "880434",
            }
            SZ_INDEX_CODES = {
                "399001", "399005", "399006", "399300", "399905", "399986", "399673", "399016",
            }
            # 6 位 ETF → 归一化成 sh/sz + 6 位
            if root.startswith(("51", "56", "58")):  # 沪市 ETF
                return f"sh{root}"
            if root.startswith(("15",)):             # 深市 ETF
                return f"sz{root}"
            if root in SZ_INDEX_CODES or root.startswith(("399",)):  # 深证指数
                return f"sz{root}"
            if root in CN_INDEX_CODES or root.startswith(("880", "881")):  # 上证指数
                return f"sh{root}"
            # 个股分支
            if root.startswith(("688", "600", "601", "603", "605", "900", "920")):
                return f"{root}.SS"
            if root.startswith(("002", "003", "001", "200", "300", "301", "000")):
                # 000 开头除了上面白名单指数，其他都是深市 A 股
                return f"{root}.SZ"
            # 未知 6 位默认走 CN-index 兜底
            return f"sh{root}"
        # 6/9 开头的个股（原始 6 位未带前缀常见形式）
        if symbol.isdigit() and len(symbol) >= 6:
            first = symbol[0]
            if first in {"6", "9", "5"}:
                return f"{symbol[:6]}.SS"
            if first in {"0", "3", "1", "2"}:
                return f"{symbol[:6]}.SZ"
        # benchmark fallback 兜底
        bm = str(benchmark or "").strip().lower()
        if bm in {"sh000300"}:
            return "sh000300"
        # 剩余完全未知 → 套 SS（当作个股处理），但保证不会变成 SH510300.SS 这种 10 字符
        stripped = symbol.replace("SH", "").replace("SZ", "").replace("sh", "").replace("sz", "")
        if len(stripped) >= 6 and stripped[:6].isdigit():
            # 用剥后的前 6 位走 SS/SZ 判定
            first = stripped[0]
            if first in {"6", "9", "5"}:
                return f"{stripped[:6]}.SS"
            if first in {"0", "3", "1", "2"}:
                return f"{stripped[:6]}.SZ"
            return f"sh{stripped[:6]}"
        return f"{symbol}.SS"
    if market == "US":
        if not symbol:
            raise ValueError("美股代码不能为空")
        return symbol.upper()
    if market == "HK":
        raw = symbol.upper()
        numeric = re.sub(r"^(?:HK[.:]?)", "", raw)
        numeric = re.sub(r"(?:[.]HK)$", "", numeric)
        if numeric.isdigit():
            return f"{_normalize_hk_symbol(raw)}.HK"
        if re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", raw):
            return raw
        raise ValueError(f"无效港股或港股指数代码 {symbol!r}")
    if market == "FUTURES":
        return _normalize_futures_symbol(symbol)
    # _normalize_market has already rejected CRYPTO/FX/unknown markets.
    raise UnsupportedOracleMarketError(f"Oracle 未配置市场 {market} 的行情路由")


def _yf_benchmark_for(benchmark: Optional[str], market: str) -> Optional[str]:
    market = _normalize_market(market)
    resolved = resolve_event_benchmark(market, benchmark)
    bm = resolved.strip().lower()
    if bm == ABSOLUTE_RETURN_BENCHMARK:
        return None
    if market == "CN":
        # UI/common vendor spelling: 000300.SH / 000905.SH.
        suffix_match = re.fullmatch(r"(\d{6})[.](sh|sz)", bm)
        if suffix_match:
            root, exchange = suffix_match.groups()
            return f"{exchange}{root}"
        # 先处理 akshare 指数直连形式：sh/sz + 6 位代码（不再强制转 ETF 代理，ETF 代理实际常缺数据）
        if len(bm) == 8 and bm[:2] in {"sh", "sz"} and bm[2:].isdigit():
            return resolved  # sh000300 / sz399006 等 → cn_index_map 处理 → _ak_cn_index_hist 通
        if bm in {"sh000300", "sz399300", "399300"}:
            return "sh000300"  # 沪深300 指数代码（ak.stock_zh_index_daily），不绕 ETF 510300.SS
        # 形如 sz399001 / sh000001 等老指数代码无 sh/sz 前缀的：如果是 6 位纯数字 → 如果是 000/399 开头指数补 sz，000001 可 sh；默认返回已知格式；未知 6 位纯数字：先走 CN 指数分支再兜底
        if len(bm) == 6 and bm.isdigit():
            # 399xxx 深证指数；000xxx 上证指数；000300 特殊=沪深300
            if bm.startswith("399"):
                return f"sz{bm}"
            if bm.startswith("000") or bm.startswith("688"):
                return f"sh{bm}"
            # 其他 6 位 → 可能是 ETF 代码（51xxxx / 15xxxx / 56xxxx）；当 CN-asset 走 _ak_cn_hist
            return bm.upper()
        # 还没命中 → 默认沪深300指数 sh000300
        return "sh000300"
    if market == "US":
        return resolved.upper()
    if market == "HK":
        # No implicit SPY: cross-market subtraction would be economically invalid.
        numeric = re.sub(r"^(?:hk[.:]?)", "", bm, flags=re.IGNORECASE)
        numeric = re.sub(r"(?:[.]hk)$", "", numeric, flags=re.IGNORECASE)
        if numeric.isdigit():
            return f"{_normalize_hk_symbol(bm)}.HK"
        value = bm.upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", value):
            raise ValueError(f"无效港股基准代码 {benchmark!r}")
        return value
    if market == "FUTURES":
        # Futures do not have one universal benchmark. Missing benchmark means
        # the Oracle labels the asset's own realized return.
        return _normalize_futures_symbol(resolved)
    raise UnsupportedOracleMarketError(f"Oracle 未配置市场 {market} 的基准路由")


@dataclass(frozen=True)
class _PriceRoute:
    provider: str
    symbol: str
    canonical: str

    @property
    def cache_key(self) -> tuple[str, str]:
        return self.provider, self.symbol


def _price_route_for(canonical_ticker: str, market: str) -> _PriceRoute:
    """Classify a canonical ticker using its declared market, never its shape alone."""
    market = _normalize_market(market)
    ticker = str(canonical_ticker or "").strip()
    if market == "US":
        return _PriceRoute("us", ticker.upper(), ticker.upper())
    if market == "HK":
        if ticker.upper().endswith(".HK"):
            code = _normalize_hk_symbol(ticker)
            return _PriceRoute("hk", code, f"{code}.HK")
        index_code = ticker.upper()
        return _PriceRoute("hk_index", index_code, index_code)
    if market == "FUTURES":
        code = _normalize_futures_symbol(ticker)
        return _PriceRoute("futures", code, code)
    if market != "CN":
        raise UnsupportedOracleMarketError(f"Oracle 未配置市场 {market} 的行情路由")

    lower = ticker.lower()
    if len(lower) == 8 and lower[:2] in {"sh", "sz"} and lower[2:].isdigit():
        return _PriceRoute("cn_index", lower, lower)
    if (ticker.endswith(".SS") or ticker.endswith(".SZ")) and len(ticker) == 9 and ticker[:6].isdigit():
        root = ticker[:6]
        if root.startswith(("51", "56", "58", "15", "399", "880", "881")):
            prefix = "sz" if root.startswith(("15", "399")) else "sh"
            return _PriceRoute("cn_index", f"{prefix}{root}", ticker)
        return _PriceRoute("cn_asset", root, ticker)
    if len(ticker) == 6 and ticker.isdigit():
        return _PriceRoute("cn_index", ticker, ticker)
    raise ValueError(f"无法识别 CN 行情代码 {canonical_ticker!r}")


@dataclass
class RawEvent:
    event_id: str
    market: str
    symbol: str
    event_date: dt.date
    event_type_l2: str = ""
    benchmark: Optional[str] = None
    event_time_raw: str = ""


def load_events(path) -> list[RawEvent]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            d = json.loads(s)
            market = str(d.get("market") or "")
            # Oracle anchoring starts when the strategy could observe the
            # information, not when the underlying event economically occurred.
            decision_time = d.get("available_time") or d.get("available_at") or d.get("event_time")
            ed = _parse_date(decision_time, market)
            if ed is None:
                continue
            rows.append(
                RawEvent(
                    event_id=str(d.get("event_id") or ""),
                    market=market,
                    symbol=str(d.get("symbol") or ""),
                    event_date=ed,
                    event_type_l2=str(d.get("event_type_l2") or ""),
                    benchmark=d.get("benchmark"),
                    event_time_raw=str(decision_time or ""),
                )
            )
    return rows


def _fetch_yf_batch(tickers: Iterable[str], start: dt.date, end: dt.date) -> dict[str, pd.Series]:
    """
    yfinance download for many tickers; return {ticker: close_series (date indexed)}
    新增：如果 batch YFRateLimitError，就拆成逐只下载（每只 sleep 1s）
    """
    ticks = sorted({t for t in tickers if t})
    if not ticks:
        return {}
    # yfinance end is exclusive, pad 2 days to ensure coverage
    end_pad = end + dt.timedelta(days=3)
    out: dict[str, pd.Series] = {}
    try:
        df = yf.download(
            tickers=ticks,
            start=start.isoformat(),
            end=end_pad.isoformat(),
            auto_adjust=False,
            progress=False,
            group_by="Ticker",
            threads=min(8, max(1, len(ticks) // 4)),
        )
    except Exception as e:
        print(f"[WARN] yf.download batch err: {type(e).__name__}: {e} -> fallback to per-ticker")
        df = None
    if df is not None:
        if len(ticks) == 1:
            t = ticks[0]
            if isinstance(df, pd.DataFrame) and "Close" in df.columns:
                s = df["Close"]
                s.index = pd.to_datetime(s.index).date
                out[t] = s.astype(float)
        else:
            for t in ticks:
                try:
                    sub = df[t] if t in df.columns.get_level_values(0) else df.xs(t, axis=1, level=0)
                    if isinstance(sub, pd.DataFrame) and "Close" in sub.columns:
                        s = sub["Close"].dropna()
                        s.index = pd.to_datetime(s.index).date
                        if len(s) > 0:
                            out[t] = s.astype(float)
                except Exception:
                    continue
    # 对于 batch 没拿到的 ticker，单独逐只下载（避免 rate-limit 封掉整批）
    missing = [t for t in ticks if t not in out or len(out.get(t, [])) == 0]
    if missing:
        print(f"[INFO] yfinance batch missing {len(missing)} tickers -> per-ticker fallback (sleep 1.6s each)")
        for idx, t in enumerate(missing):
            # 更长 sleep + 最多 3 次指数退避重试
            ok = False
            for attempt in range(1, 4):
                try:
                    d2 = yf.download(
                        tickers=[t],
                        start=start.isoformat(),
                        end=end_pad.isoformat(),
                        auto_adjust=False,
                        progress=False,
                        threads=1,
                    )
                    if isinstance(d2, pd.DataFrame) and "Close" in d2.columns:
                        s = d2["Close"].dropna()
                        if len(s) > 0:
                            s.index = pd.to_datetime(s.index).date
                            out[t] = s.astype(float)
                            ok = True
                    if ok:
                        break
                except Exception as e:
                    msg = f"{type(e).__name__}: {e}"
                    print(f"[WARN] yf per-ticker {t} attempt={attempt} err: {msg}")
                    bo = (2.0 ** (attempt - 1)) * 2.1
                    time.sleep(bo)
                    continue
                # 没抛异常但也没拿到数据：再等一次长 sleep
                time.sleep(1.3 + 0.4 * attempt)
            time.sleep(1.6 if ok else 3.4)
    return out


def _announcement_tier(event_time_str: str, market: str) -> str:
    """
    判断公告时段：返回 "pre_open" / "intraday" / "post_close"
    - CN: pre_open < 09:30, intraday = [09:30, 15:00), post_close >= 15:00
    - US: pre_open < 09:30, intraday = [09:30, 16:00), post_close >= 16:00
    - 无时间组件 / 不可解析 -> "unknown"（按下一交易日收盘保守锚定）
    """
    s = str(event_time_str or "").strip()
    if not s:
        return "unknown"
    # 检测是否含时间组件：ISO datetime 用 'T' 分隔；也兼容空格分隔的 "YYYY-MM-DD HH:MM:SS"
    # date-only 如 "2025-01-10" / "20250110" / "2025/01/10" -> 当作无时间组件
    has_time = ("T" in s) or ("t" in s) or (len(s) > 10 and ":" in s[10:])
    if not has_time:
        return "unknown"
    obj = _event_datetime_in_market(s, market)
    if obj is None:
        return "unknown"
    t = obj.time()
    mkt = (market or "").upper()
    if mkt in {"CN", "FUTURES"}:
        open_t = dt.time(9, 30)
        close_t = dt.time(15, 0)
    else:  # US / HK / 未知
        open_t = dt.time(9, 30)
        close_t = dt.time(16, 0)
    if t < open_t:
        return "pre_open"
    if t >= close_t:
        return "post_close"
    return "intraday"


def _event_anchor_dates(
    closes: pd.Series,
    event_date: dt.date,
    window: int,
    event_time_raw: str = "",
    market: str = "",
) -> Optional[tuple[dt.date, dt.date, int, int]]:
    """Resolve the information-safe close-to-close Oracle window.

    Pre-open/intraday information enters at that trading day's close. Post-close
    (or date-only, whose publication time is unknown) enters at the next trading
    close. A weekend/holiday already maps to the first later trading day and must
    not be shifted a second time.
    """
    if closes is None or len(closes) == 0 or int(window) < 1:
        return None
    dates_avail = sorted(closes.index)
    t0_candidates = [d for d in dates_avail if d >= event_date]
    if not t0_candidates:
        return None
    t0 = t0_candidates[0]
    t0_idx = dates_avail.index(t0)
    tier = _announcement_tier(event_time_raw, market)
    if tier in {"post_close", "unknown"} and t0 == event_date:
        if t0_idx + 1 >= len(dates_avail):
            return None
        t0_idx += 1
        t0 = dates_avail[t0_idx]
    tN_idx = t0_idx + int(window)
    if tN_idx >= len(dates_avail):
        return None
    return t0, dates_avail[tN_idx], t0_idx, tN_idx


def _return_between(closes: Optional[pd.Series], start: dt.date, end: dt.date) -> Optional[float]:
    if closes is None or start not in closes.index or end not in closes.index:
        return None
    p0 = float(closes.loc[start])
    pN = float(closes.loc[end])
    if not p0 or not math.isfinite(p0) or not math.isfinite(pN):
        return None
    return (pN / p0) - 1.0


def _car(closes: pd.Series, event_date: dt.date, window: int,
         event_time_raw: str = "", market: str = "") -> Optional[float]:
    """
    event-t0 = 收盘前发布公告 -> 用 event_date 当天 close 作为基准；
    horizon = T+N 的 close。
    若公告时段为 post_close -> T0 顺延到下一个交易日。
    """
    if closes is None or len(closes) == 0:
        return None
    anchors = _event_anchor_dates(closes, event_date, window, event_time_raw, market)
    if anchors is None:
        return None
    t0, tN, _, _ = anchors
    return _return_between(closes, t0, tN)


def _market_model_car_with_method(stock_closes: pd.Series, index_closes: pd.Series,
                      event_date: dt.date, window: int,
                      event_time_raw: str = "", market: str = ""
                      ) -> tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
    """
    市场模型 CAR（OLS 估计 α/β，替代 β≡1 的简单减法）：
    - T0 同 _car 的 post_close 偏移规则
    - 估计窗口: [T0-120, T0-21] 交易日（100 个）
    - 事件窗口: (T0, T0+window]，AR_t = r_stock_t - (α̂ + β̂ * r_index_t)
    - CAR = Σ AR_t（共 window 项，与 close(T0)→close(T+N) 端点口径一致）
    - σ(AR) 取自估计窗口残差
    - t_stat = CAR / (σ_AR * sqrt(window))
    - p_value = 2 * (1 - Φ(|t_stat|))，Φ 用 math.erf 近似
    - 估计窗口数据 < 30 -> 退回端点法 CAR = (pN/p0 - 1) - (bmN/bm0 - 1)，t_stat=p_value=None
    - 返回 (CAR, t_stat, p_value, 实际计算方法)；数据缺失时全部为 None
    """
    empty = (None, None, None, None)
    if stock_closes is None or index_closes is None or len(stock_closes) == 0 or len(index_closes) == 0:
        return empty

    dates_avail = sorted(stock_closes.index)
    anchors = _event_anchor_dates(stock_closes, event_date, window, event_time_raw, market)
    if anchors is None:
        return empty
    t0, tN, t0_idx, tN_idx = anchors

    def _fallback_endpoint() -> tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
        r_a = _return_between(stock_closes, t0, tN)
        # Require the exact asset anchor dates. Subtracting benchmark returns
        # over a different calendar (e.g. after a suspension) is invalid.
        r_b = _return_between(index_closes, t0, tN)
        if r_a is None or r_b is None:
            return empty
        return (r_a - r_b, None, None, "benchmark_relative_return")

    # 估计窗口 [t0_idx-120, t0_idx-21] 闭区间（100 个交易日）
    est_lo = t0_idx - 120
    est_hi = t0_idx - 21  # inclusive
    if est_hi < 0:
        return _fallback_endpoint()
    est_lo_c = max(0, est_lo)
    if est_hi < est_lo_c:
        return _fallback_endpoint()
    est_dates = dates_avail[est_lo_c : est_hi + 1]
    if len(est_dates) < 30:
        return _fallback_endpoint()

    # 对齐 stock / index 收盘价
    stock_est = stock_closes.reindex(est_dates).dropna()
    index_est = index_closes.reindex(est_dates).dropna()
    common = stock_est.index.intersection(index_est.index)
    if len(common) < 30:
        return _fallback_endpoint()
    stock_est = stock_est.loc[common]
    index_est = index_est.loc[common]
    r_stock_est = stock_est.pct_change().dropna()
    r_index_est = index_est.pct_change().dropna()
    common2 = r_stock_est.index.intersection(r_index_est.index)
    if len(common2) < 30:
        return _fallback_endpoint()
    r_stock_arr = r_stock_est.loc[common2].to_numpy(dtype=float)
    r_index_arr = r_index_est.loc[common2].to_numpy(dtype=float)

    # OLS: r_stock = α + β * r_index (numpy.polyfit degree=1)
    try:
        beta, alpha = np.polyfit(r_index_arr, r_stock_arr, 1)
    except Exception:
        return _fallback_endpoint()
    if not (math.isfinite(float(alpha)) and math.isfinite(float(beta))):
        return _fallback_endpoint()
    pred = alpha + beta * r_index_arr
    resid = r_stock_arr - pred
    if len(resid) < 2:
        return _fallback_endpoint()
    sigma_ar = float(np.std(resid, ddof=1))
    if not math.isfinite(sigma_ar) or sigma_ar <= 0.0:
        return _fallback_endpoint()

    # close(T0) -> close(T+N): N returns from N+1 close observations.
    ev_price_dates = dates_avail[t0_idx : tN_idx + 1]
    if len(ev_price_dates) < window + 1:
        return _fallback_endpoint()
    stock_ev = stock_closes.reindex(ev_price_dates).dropna()
    index_ev = index_closes.reindex(ev_price_dates).dropna()
    common_ev = stock_ev.index.intersection(index_ev.index)
    if len(common_ev) < window + 1:
        return _fallback_endpoint()
    stock_ev = stock_ev.loc[common_ev]
    index_ev = index_ev.loc[common_ev]
    r_stock_ev = stock_ev.pct_change().dropna().to_numpy(dtype=float)  # window returns
    r_index_ev = index_ev.pct_change().dropna().to_numpy(dtype=float)
    if len(r_stock_ev) != window or len(r_index_ev) != window:
        return _fallback_endpoint()
    ar_ev = r_stock_ev - (alpha + beta * r_index_ev)
    car = float(np.sum(ar_ev))
    denom = sigma_ar * math.sqrt(window)
    if not math.isfinite(denom) or denom <= 0:
        return car, None, None, "market_model"
    t_stat = car / denom
    # p_value = 2 * (1 - Φ(|t|))，Φ 用 math.erf 近似
    p_value = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t_stat) / math.sqrt(2.0))))
    if p_value < 0.0:
        p_value = 0.0
    elif p_value > 1.0:
        p_value = 1.0
    return car, float(t_stat), float(p_value), "market_model"


def _market_model_car(stock_closes: pd.Series, index_closes: pd.Series,
                      event_date: dt.date, window: int,
                      event_time_raw: str = "", market: str = ""
                      ) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Compatibility entry point for explicit OLS callers."""
    car, t_stat, p_value, _ = _market_model_car_with_method(
        stock_closes, index_closes, event_date, window, event_time_raw, market,
    )
    return car, t_stat, p_value


def _compute_cars_for_events(
    events: list[RawEvent],
    *, car_method: str = "benchmark_relative_return",
) -> dict[tuple, dict]:
    """
    New labels use endpoint benchmark-relative returns, matching the prediction
    prompt. OLS is opt-in; its actual method is tracked per horizon so an
    endpoint fallback is never mislabeled as an OLS result.

    returns: {(event_id, market, symbol): {
        "t1": float|None, "t3": float|None, "t5": float|None,
        "bm_t1": ..., "bm_t3": ..., "bm_t5": ...,
        "car_t1": ..., "car_t3": ..., "car_t5": ...,
        "asset_ticker": str, "benchmark_ticker": str,
    }}
    """
    if car_method not in {"benchmark_relative_return", "market_model"}:
        raise ValueError("car_method must be benchmark_relative_return or market_model")
    if not events:
        return {}

    # Validate and route the complete batch before touching a remote provider.
    # CRYPTO/FX therefore fail atomically instead of producing partial labels.
    asset_routes: dict[tuple, _PriceRoute] = {}
    benchmark_routes: dict[tuple, Optional[_PriceRoute]] = {}
    unique_routes: dict[tuple[str, str], _PriceRoute] = {}
    for e in events:
        market = _normalize_market(e.market)
        key = (e.event_id, e.market, e.symbol)
        asset_ticker = _yf_ticker_for(e.symbol, market, e.benchmark)
        asset_route = _price_route_for(asset_ticker, market)
        benchmark_ticker = _yf_benchmark_for(e.benchmark, market)
        benchmark_route = (
            _price_route_for(benchmark_ticker, market) if benchmark_ticker else None
        )
        asset_routes[key] = asset_route
        benchmark_routes[key] = benchmark_route
        unique_routes[asset_route.cache_key] = asset_route
        if benchmark_route is not None:
            unique_routes[benchmark_route.cache_key] = benchmark_route

    # Market-model estimation needs ~120 trading days before T0; T+60 needs
    # roughly another 120 calendar days after the latest event.
    earliest = min(e.event_date for e in events) - dt.timedelta(days=200)
    latest = max(e.event_date for e in events) + dt.timedelta(days=150)
    sd_iso = earliest.isoformat()
    ed_iso = latest.isoformat()

    by_provider = Counter(route.provider for route in unique_routes.values())
    print(
        f"[INFO] Close universe: {len(unique_routes)} routes {dict(by_provider)}; "
        f"window {sd_iso} ~ {ed_iso}"
    )
    fetchers = {
        "us": _ak_us_hist,
        "cn_asset": _ak_cn_hist,
        "cn_index": _ak_cn_index_hist,
        "hk": _ak_hk_hist,
        "hk_index": _ak_hk_index_hist,
        "futures": _ak_futures_hist,
    }
    closes_by_route: dict[tuple[str, str], pd.Series] = {}
    for index, route in enumerate(unique_routes.values(), start=1):
        series = fetchers[route.provider](route.symbol, sd_iso, ed_iso)
        if series is not None and len(series) > 0:
            closes_by_route[route.cache_key] = series
        print(
            f"[PROG] Oracle prices {index}/{len(unique_routes)} "
            f"provider={route.provider} coverage={len(closes_by_route)}/{len(unique_routes)}"
        )

    print(
        f"[INFO] Final close coverage (akshare-only): "
        f"{len(closes_by_route)}/{len(unique_routes)} routes"
    )

    out: dict[tuple, dict] = {}
    no_asset = 0
    no_bm = 0
    raw_asset_return = 0
    HORIZONS: list[tuple[int, str]] = [(1, "t1"), (3, "t3"), (5, "t5"), (7, "t7"), (15, "t15"), (30, "t30"), (60, "t60")]
    for e in events:
        key = (e.event_id, e.market, e.symbol)
        market = _normalize_market(e.market)
        asset_route = asset_routes[key]
        benchmark_route = benchmark_routes[key]
        a_close = closes_by_route.get(asset_route.cache_key)
        b_close = (
            closes_by_route.get(benchmark_route.cache_key)
            if benchmark_route is not None else None
        )
        rec: dict = {
            "asset_ticker": asset_route.canonical,
            "benchmark_ticker": benchmark_route.canonical if benchmark_route else None,
            "car_method": car_method if benchmark_route else "raw_asset_return",
            "car_method_requested": car_method if benchmark_route else "raw_asset_return",
            "car_methods": {},
        }
        # initialize horizons
        for _, kn in HORIZONS:
            rec[kn] = None
            rec[f"bm_{kn}"] = None
            rec[f"car_{kn}"] = None
            rec[f"car_{kn}_tstat"] = None
            rec[f"car_{kn}_pvalue"] = None
        if a_close is None:
            no_asset += 1
        if benchmark_route is not None and b_close is None:
            no_bm += 1
        if benchmark_route is None:
            raw_asset_return += 1
        for w, key_name in HORIZONS:
            anchors = (
                _event_anchor_dates(a_close, e.event_date, w, e.event_time_raw, market)
                if a_close is not None else None
            )
            if anchors is None:
                r_a = None
                r_b = None
            else:
                anchor_start, anchor_end, _, _ = anchors
                r_a = _return_between(a_close, anchor_start, anchor_end)
                r_b = _return_between(b_close, anchor_start, anchor_end)
            rec[key_name] = r_a
            rec[f"bm_{key_name}"] = r_b
            if benchmark_route is None:
                # No benchmark is an explicit absolute-return protocol. Never
                # inject SPY (or any synthetic series) across markets.
                rec[f"car_{key_name}"] = r_a
                if r_a is not None:
                    rec["car_methods"][key_name] = "raw_asset_return"
            elif car_method == "benchmark_relative_return":
                # The prediction contract asks whether the asset outperforms
                # its benchmark over these exact same close-to-close anchors.
                # Missing benchmark endpoints must not become a zero return.
                if r_a is not None and r_b is not None:
                    rec[f"car_{key_name}"] = r_a - r_b
                    rec["car_methods"][key_name] = "benchmark_relative_return"
            elif a_close is not None and b_close is not None:
                car, t_stat, p_value, actual_method = _market_model_car_with_method(
                    a_close, b_close, e.event_date, w, e.event_time_raw, market
                )
                rec[f"car_{key_name}"] = car
                rec[f"car_{key_name}_tstat"] = t_stat
                rec[f"car_{key_name}_pvalue"] = p_value
                if actual_method is not None:
                    rec["car_methods"][key_name] = actual_method
        actual_methods = set(rec["car_methods"].values())
        if len(actual_methods) == 1:
            rec["car_method"] = next(iter(actual_methods))
        elif len(actual_methods) > 1:
            rec["car_method"] = "market_model_with_endpoint_fallback"
        out[key] = rec
    if no_asset or no_bm:
        print(f"[WARN] Missing closes: asset={no_asset}, benchmark={no_bm}")
    if raw_asset_return:
        print(f"[INFO] Absolute-return Oracle (no benchmark): {raw_asset_return}/{len(events)} events")
    return out


def _label_from_car(car: Optional[float], epsilon: float) -> str:
    if car is None or not math.isfinite(car):
        return ""
    if car > epsilon:
        return "up"
    if car < -epsilon:
        return "down"
    # With a zero threshold, a genuinely flat outcome has no directional
    # ground truth. Keep it out of direction accuracy instead of guessing.
    return "" if epsilon == 0 else "neutral"


def write_labels(events: list[RawEvent], cars: dict, out_path, epsilon: float = 0.005):
    HORIZONS_DIR: list[str] = ["t1", "t3", "t5", "t7", "t15", "t30", "t60"]
    # horizons used for "direction evidence" aggregations (exclude t1 which is too noisy)
    AGG_H: list[str] = ["t3", "t7", "t15", "t30", "t60"]

    def _sign(x: Optional[float]) -> int:
        if x is None or not math.isfinite(x): return 0
        if x > epsilon: return 1
        if x < -epsilon: return -1
        return 0

    def _mean_nonnull(vals: list[Optional[float]]) -> Optional[float]:
        xs = [v for v in vals if v is not None and math.isfinite(v)]
        if not xs: return None
        return float(sum(xs) / len(xs))

    def _weighted_mean_nonnull(vals: list[tuple[Optional[float], float]]) -> Optional[float]:
        total = 0.0; weight = 0.0
        for v, w in vals:
            if v is not None and math.isfinite(v):
                total += v * w; weight += w
        if weight <= 0: return None
        return total / weight

    rows = []
    for e in events:
        key = (e.event_id, e.market, e.symbol)
        c = cars.get(key) or {}
        p_t3 = c.get("car_t3_pvalue")
        sig_t3 = bool(p_t3 is not None and p_t3 < 0.10)

        row: dict = {
            "event_id": e.event_id,
            "market": e.market,
            "symbol": e.symbol,
            "event_time": e.event_date.isoformat(),
            "event_type_l2": e.event_type_l2,
            "asset_ticker": c.get("asset_ticker"),
            "benchmark_ticker": c.get("benchmark_ticker"),
            "car_method": c.get("car_method"),
            "car_method_requested": c.get("car_method_requested"),
            "car_methods": c.get("car_methods") or {},
            "epsilon": float(epsilon),
        }
        # ---- horizon fields (ret / bm_ret / car / car_pvalue for each) ----
        for kn in HORIZONS_DIR:
            row[f"ret_{kn}"] = c.get(kn)
            row[f"bm_ret_{kn}"] = c.get(f"bm_{kn}")
            row[f"car_{kn}"] = c.get(f"car_{kn}")
            row[f"car_{kn}_pvalue"] = c.get(f"car_{kn}_pvalue")
        row["sig_t3"] = sig_t3

        # ---- individual horizon labels (compatibility) ----
        for kn in HORIZONS_DIR:
            row[f"label_{kn}"] = _label_from_car(c.get(f"car_{kn}"), epsilon)

        # ---- aggregations: avgCARs across non-t1 horizons ----
        # short (t3,t7), mid (t3,t7,t15), long (t15,t30,t60), all (t3,t7,t15,t30,t60)
        cars_short = [c.get(f"car_{h}") for h in ["t3", "t7"]]
        cars_mid = [c.get(f"car_{h}") for h in ["t3", "t7", "t15"]]
        cars_long = [c.get(f"car_{h}") for h in ["t15", "t30", "t60"]]
        cars_all = [c.get(f"car_{h}") for h in AGG_H]

        row["car_avg_short"] = _mean_nonnull(cars_short)
        row["car_avg_mid"] = _mean_nonnull(cars_mid)
        row["car_avg_long"] = _mean_nonnull(cars_long)
        # all-horizon weighted avg: nearer horizons get slightly more weight (we care most
        # about short-term reaction, but want long-term direction consistency to smooth noise)
        row["car_avg_all"] = _weighted_mean_nonnull(list(zip(
            cars_all,
            [0.35, 0.28, 0.20, 0.12, 0.05],  # weights sum=1.0; t3=0.35 t7=0.28 t15=0.20 t30=0.12 t60=0.05
        )))

        # how many of the 5 AGG horizons actually have valid CAR
        n_valid = sum(1 for v in cars_all if v is not None and math.isfinite(v))
        row["n_horizons_valid"] = n_valid

        # direction consensus signals (among AGG_H with valid sign)
        signs = [s for s in [_sign(c.get(f"car_{h}")) for h in AGG_H] if s != 0]
        row["n_horizons_signed"] = len(signs)
        if signs:
            up_cnt = sum(1 for s in signs if s > 0)
            down_cnt = sum(1 for s in signs if s < 0)
            maj = max(up_cnt, down_cnt)
            row["consensus_up_frac"] = (up_cnt / len(signs)) if signs else None
            row["consensus_down_frac"] = (down_cnt / len(signs)) if signs else None
            # max direction agreement: 1.0 = all same sign
            row["consensus_maj_frac"] = (maj / len(signs)) if signs else None
            # -1..+1 net: up_frac - down_frac; used as stable direction evidence
            row["consensus_net"] = ((up_cnt - down_cnt) / len(signs)) if signs else None
        else:
            row["consensus_up_frac"] = None
            row["consensus_down_frac"] = None
            row["consensus_maj_frac"] = None
            row["consensus_net"] = None

        # avg-based labels (the new "Oracle direction" for scoring / training)
        row["label_avg_short"] = _label_from_car(row["car_avg_short"], epsilon)
        row["label_avg_mid"] = _label_from_car(row["car_avg_mid"], epsilon)
        row["label_avg_long"] = _label_from_car(row["car_avg_long"], epsilon)
        row["label_avg_all"] = _label_from_car(row["car_avg_all"], epsilon)
        # label by strict consensus (>=66% horizons agree in same direction; else neutral)
        if signs and (row.get("consensus_maj_frac") or 0.0) >= 0.66 and row.get("consensus_net") is not None:
            net = row["consensus_net"]
            if net > 0: row["label_consensus66"] = "up"
            elif net < 0: row["label_consensus66"] = "down"
            else: row["label_consensus66"] = "" if epsilon == 0 else "neutral"
        else:
            row["label_consensus66"] = "" if epsilon == 0 else "neutral"

        rows.append(row)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    print(f"[INFO] labels written -> {out_path} ({len(rows)} rows)")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True, help="events_phase1.jsonl")
    ap.add_argument("--out", required=True, help="labels.jsonl")
    ap.add_argument("--epsilon", type=float, default=0.0, help="direction threshold (default 0: up/down; exact zero has no direction); set positive for a legacy neutral band")
    ap.add_argument(
        "--car-method", choices=("benchmark_relative_return", "market_model"),
        default="benchmark_relative_return",
        help="direction target: endpoint benchmark-relative return (default), or explicit legacy OLS CAR",
    )
    args = ap.parse_args()

    events = load_events(args.events)
    print(f"[INFO] loaded {len(events)} events from {args.events}")
    if not events:
        raise SystemExit("no events")
    cars = _compute_cars_for_events(events, car_method=args.car_method)
    _rows = write_labels(events, cars, args.out, epsilon=float(args.epsilon))
    # summary stats
    mkt = Counter(e.market for e in events)
    def _lc(name): return Counter(r[name] for r in _rows if r.get(name))
    print(f"[INFO] market: {dict(mkt)}")
    for ln in ["label_t3", "label_t7", "label_t15", "label_t30", "label_t60",
               "label_avg_short", "label_avg_mid", "label_avg_long", "label_avg_all", "label_consensus66"]:
        dist = dict(_lc(ln))
        if dist: print(f"[INFO] {ln} distribution (eps={args.epsilon}): {dist}")
    # horizon coverage
    for h in ["t3", "t7", "t15", "t30", "t60"]:
        nv = sum(1 for r in _rows if r.get(f"car_{h}") is not None)
        print(f"[INFO] car_{h} coverage: {nv}/{len(_rows)} ({nv*100//len(_rows) if _rows else 0}%)")
    n_valid_stats = Counter(r["n_horizons_valid"] for r in _rows)
    print(f"[INFO] n_horizons_valid distribution: {dict(sorted(n_valid_stats.items()))}")
    # avgCAR stats
    for name in ["car_avg_short", "car_avg_mid", "car_avg_long", "car_avg_all"]:
        vs = [r[name] for r in _rows if r.get(name) is not None]
        if vs:
            arr = np.array(vs, dtype=float) * 10000  # → bps
            print(f"[INFO] {name} (bps): n={len(arr)} mean={arr.mean():+.1f} med={np.median(arr):+.1f} std={arr.std():.0f} p5={np.percentile(arr,5):.0f} p95={np.percentile(arr,95):.0f}")
    # cross-label agreement: label_t3 vs label_avg_all
    agree = sum(1 for r in _rows if r.get("label_t3") and r.get("label_avg_all") and r["label_t3"] == r["label_avg_all"])
    total_both = sum(1 for r in _rows if r.get("label_t3") and r.get("label_avg_all"))
    if total_both:
        print(f"[INFO] label_t3 <-> label_avg_all 方向一致性: {agree}/{total_both} = {agree*100//total_both}%")
    print("DONE_LABELS")


if __name__ == "__main__":
    main()
