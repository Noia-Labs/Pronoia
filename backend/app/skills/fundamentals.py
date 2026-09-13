"""Fundamentals & macro skills (design.md §4):
get_financial_abstract / get_financial_indicator / get_research_reports /
get_lhb / get_margin / get_macro — whitelisted akshare interfaces only.

美股支持：akshare.stock_us_spot_em 网络极不稳定，US 路径改为「K线派生 + 静默兜底」——
- get_financial_abstract：先试 spot_em 拿当前价/PE/市值，失败/为美股则用 K线派生（最新价、
  5/20/60 日 MA、30 日收益、60 日年化波动率、近 252 日最大回撤）。
- get_financial_indicator：US 用 K线派生（MA5/20/60、收益、波动率、回撤）；A 股原样。
- get_research_reports：US 无公开研报接口，返回空 + note 指向外部数据源建议。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import akshare as ak
import pandas as pd

from ..market_runtime import call_sina_history
from .market import is_us_symbol, norm_date, norm_us_symbol
from .registry import err, json_safe, meta, ok, skill


def _code6(symbol: str) -> str:
    code = "".join(ch for ch in str(symbol) if ch.isdigit())[-6:]
    if len(code) != 6:
        raise ValueError(f"无法识别股票代码: {symbol}")
    return code


def _table_artifact(title: str, records: list[dict], note: str | None = None) -> dict:
    payload: dict = {"columns": list(records[0].keys()),
                     "rows": [[r.get(c) for c in records[0].keys()] for r in records]}
    if note:
        payload["note"] = note
    return {"kind": "table", "title": title, "payload": payload}


# --------------------------------------------------- US 派生指标 helper ----


def _fetch_us_kline_df(sym: str) -> pd.DataFrame:
    """取美股最近 250 行日K（已排序/清洗）。失败抛 ValueError。"""
    from .market import _clean_ohlcv
    df = call_sina_history(ak.stock_us_daily, symbol=sym, adjust="qfq")
    if df is None or len(df) == 0:
        raise ValueError(f"美股 {sym} 无日K数据")
    df, _ = _clean_ohlcv(df, limit=250)
    return df


def _try_us_spot(sym: str) -> Optional[dict]:
    """尝试 stock_us_spot_em 拿当前价/PE/市值/换手率等。失败返回 None。"""
    try:
        df = ak.stock_us_spot_em(symbol=sym)
    except Exception:  # noqa: BLE001
        return None
    if df is None or len(df) == 0:
        return None
    row = df.iloc[0].to_dict()
    return {str(k).strip(): v for k, v in row.items()}


def _derive_us_indicators(df: pd.DataFrame) -> list[dict]:
    """从美股 K线派生常用技术/收益类指标。返回多期 records（最新→最旧）。"""
    closes = pd.to_numeric(df["close"], errors="coerce").dropna().reset_index(drop=True)
    n = len(closes)
    if n < 5:
        return []

    def _ma(window: int) -> Optional[float]:
        if n < window:
            return None
        return round(float(closes.tail(window).mean()), 4)

    def _cum_return(days: int) -> Optional[float]:
        if n < days + 1:
            return None
        c_now = float(closes.iloc[-1])
        c_then = float(closes.iloc[-(days + 1)])
        if c_then == 0:
            return None
        return round((c_now - c_then) / c_then * 100.0, 4)

    def _vol_annualized(days: int) -> Optional[float]:
        if n < days + 1:
            return None
        rets = closes.pct_change().tail(days).dropna()
        if len(rets) < 5:
            return None
        return round(float(rets.std() * (252 ** 0.5) * 100.0), 4)

    def _max_drawdown(days: int) -> Optional[float]:
        if n < days:
            window = closes
        else:
            window = closes.tail(days)
        if len(window) < 2:
            return None
        cummax = window.cummax()
        dd = (window - cummax) / cummax
        return round(float(dd.min() * 100.0), 4)

    # 仅输出一期「最新」记录——美股没有财报期概念
    latest_price = round(float(closes.iloc[-1]), 4)
    asof = str(df["date"].iloc[-1])
    rec = {
        "期间": asof,
        "最新价": latest_price,
        "MA5": _ma(5),
        "MA20": _ma(20),
        "MA60": _ma(60),
        "30日收益(%)": _cum_return(30),
        "60日收益(%)": _cum_return(60),
        "60日年化波动率(%)": _vol_annualized(60),
        "252日最大回撤(%)": _max_drawdown(252),
    }
    return [rec]


def _enrich_us_indicators_with_spot(records: list[dict], spot: Optional[dict]) -> list[dict]:
    """如果 spot 数据可用，把当前价/PE/市值/换手率合并进首期记录。"""
    if not records or not spot:
        return records
    out = [dict(records[0])]
    rest = list(records[1:])
    # 不同 akshare 版本列名：中文/英文都可能
    for src_key, dst_key in (
        ("最新价", "现价(spot)"),
        ("now", "现价(spot)"),
        ("price", "现价(spot)"),
        ("市值", "市值"),
        ("market_cap", "市值"),
        ("market_capital", "市值"),
        ("市盈率", "市盈率(TTM)"),
        ("pe", "市盈率(TTM)"),
        ("pe_ttm", "市盈率(TTM)"),
        ("市净率", "市净率"),
        ("pb", "市净率"),
        ("换手率", "换手率(%)"),
        ("turnover", "换手率(%)"),
        ("成交量", "成交量(spot)"),
        ("volume", "成交量(spot)"),
    ):
        v = spot.get(src_key)
        if v is None:
            continue
        try:
            out[0][dst_key] = round(float(v), 4) if isinstance(v, (int, float)) else v
        except (TypeError, ValueError):
            out[0][dst_key] = v
    return out + rest


# ------------------------------------------------------- financials ------


@skill(
    "get_financial_abstract",
    "获取个股财务摘要（常用指标最近5期转置表：营收/净利润/ROE/毛利率/资产负债率等）。"
    "symbol 为 6 位 A 股代码或美股 ticker（如 AAPL）。"
    "美股无公开财务摘要，改为 K线派生指标（MA5/20/60、收益、波动率、回撤），并尝试"
    "akshare.stock_us_spot_em 拿当前价/PE/市值/换手率（失败静默降级）。",
    {
        "type": "object",
        "properties": {"symbol": {"type": "string",
                                  "description": "6 位 A 股代码 / 美股 ticker (AAPL/TSLA/NVDA)"}},
        "required": ["symbol"],
    },
    internal=True,)
def get_financial_abstract(symbol: str) -> dict:
    # 美股路径：K线派生 + spot 静默兜底
    if is_us_symbol(symbol):
        try:
            sym = norm_us_symbol(symbol)
            df = _fetch_us_kline_df(sym)
        except ValueError as e:
            return err(str(e))
        except Exception as e:  # noqa: BLE001
            return err(f"美股日K获取失败: {type(e).__name__}: {e}")
        records = _derive_us_indicators(df)
        if not records:
            return err(f"美股 {sym} 数据不足，无法派生指标")
        spot = _try_us_spot(sym)
        records = _enrich_us_indicators_with_spot(records, spot)
        m = meta("akshare.stock_us_daily+derived", len(records))
        if spot:
            m["spot_enriched"] = True
        m["market"] = "美股"
        return ok(
            records, m,
            artifact=_table_artifact(
                f"{sym} 美股技术/收益摘要（{records[0].get('期间')}）", records,
                note="美股无公开财务摘要，本表由 K线 派生；如现价/PE 列存在表示 spot 兜底成功",
            ),
        )
    # A 股路径：原样
    try:
        code = _code6(symbol)
        df = ak.stock_financial_abstract(symbol=code)
        if df is None or len(df) == 0:
            return err(f"{code} 无财务摘要数据")
        date_cols = [c for c in df.columns if str(c).isdigit()][:5]  # 最近5期（新→旧）
        keep_indicators = [
            "归母净利润", "营业总收入", "净利润", "扣非净利润", "经营现金流量净额",
            "基本每股收益", "每股净资产", "净资产收益率(ROE)", "毛利率", "销售净利率",
            "资产负债率", "总资产报酬率(ROA)",
        ]
        sub = df[df["指标"].isin(keep_indicators)].drop_duplicates(subset=["指标"])
        sub = sub.set_index("指标").reindex([i for i in keep_indicators if i in set(sub["指标"])])
        periods_asc = sorted(date_cols)  # 旧→新，便于阅读
        records = []
        for ind, row in sub.iterrows():
            rec = {"指标": ind}
            for p in periods_asc:
                rec[p] = json_safe(row.get(p))
            records.append(rec)
        if not records:
            return err(f"{code} 财务摘要缺少常用指标")
        return ok(
            records,
            meta("akshare.stock_financial_abstract", len(records)),
            artifact=_table_artifact(f"{code} 财务摘要（最近 {len(periods_asc)} 期）", records,
                                     note="报告期格式 YYYYMMDD"),
        )
    except Exception as e:  # noqa: BLE001
        return err(f"财务摘要获取失败: {type(e).__name__}: {e}")


@skill(
    "get_financial_indicator",
    "获取个股财务指标时间序列（12个核心列：EPS/ROE/净利率/毛利率/资产负债率/增长率等），按年度。"
    "美股无年度财务指标接口，US 路径改为 K线派生：MA5/20/60、30/60 日收益、年化波动率、最大回撤。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string",
                       "description": "6 位 A 股代码 / 美股 ticker (AAPL/TSLA/NVDA)"},
            "start_year": {"type": "string", "description": "起始年份（A 股）；美股忽略此参数"},
        },
        "required": ["symbol"],
    },
    internal=True,)
def get_financial_indicator(symbol: str, start_year: Optional[str] = None) -> dict:
    # 美股路径：K线派生（与 get_financial_abstract US 分支相同的指标集，但横轴是「近 N 日」快照）
    if is_us_symbol(symbol):
        try:
            sym = norm_us_symbol(symbol)
            df = _fetch_us_kline_df(sym)
        except ValueError as e:
            return err(str(e))
        except Exception as e:  # noqa: BLE001
            return err(f"美股日K获取失败: {type(e).__name__}: {e}")
        records = _derive_us_indicators(df)
        if not records:
            return err(f"美股 {sym} 数据不足，无法派生指标")
        m = meta("akshare.stock_us_daily+derived", len(records))
        m["market"] = "美股"
        return ok(
            records, m,
            artifact=_table_artifact(
                f"{sym} 美股技术/收益指标（{records[0].get('期间')}）", records,
                note="美股无年度财报接口；本表由 K线 派生最新一期",
            ),
        )
    # A 股路径：原样
    try:
        code = _code6(symbol)
        start_year = (start_year or str(date.today().year - 2))[:4]
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year=start_year)
        if df is None or len(df) == 0:
            return err(f"{code} 自 {start_year} 起无财务指标数据")
        core = ["日期", "摊薄每股收益(元)", "每股净资产_调整后(元)", "每股经营性现金流(元)",
                "净资产收益率(%)", "销售净利率(%)", "销售毛利率(%)", "主营业务利润率(%)",
                "资产负债率(%)", "流动比率", "主营业务收入增长率(%)", "净利润增长率(%)"]
        cols = [c for c in core if c in df.columns]
        df = df[cols].copy()
        df["日期"] = df["日期"].astype(str).str[:10]
        df = df.sort_values("日期")
        if len(df) > 24:
            df = df.tail(24)
        records = json_safe(df.to_dict(orient="records"))
        return ok(
            records,
            meta("akshare.stock_financial_analysis_indicator", len(records)),
            artifact=_table_artifact(f"{code} 财务指标（{start_year} 起）", records),
        )
    except Exception as e:  # noqa: BLE001
        return err(f"财务指标获取失败: {type(e).__name__}: {e}")


@skill(
    "get_research_reports",
    "获取个股最近券商研报与评级（机构、评级、盈利预测、报告链接），限10条。"
    "A 股用 akshare.stock_research_report_em；美股无对应接口，返回空 + 外部数据源建议。",
    {
        "type": "object",
        "properties": {"symbol": {"type": "string",
                                  "description": "6 位 A 股代码 / 美股 ticker"}},
        "required": ["symbol"],
    },
    internal=True,)
def get_research_reports(symbol: str) -> dict:
    # 美股路径：akshare 无 US 研报接口，给出友好提示
    if is_us_symbol(symbol):
        sym = norm_us_symbol(symbol)
        m = meta("us.no_research_report_api", 0)
        m["market"] = "美股"
        return ok(
            [],
            m,
        ) | {"note": (f"美股 {sym} 无公开研报接口。建议改用以下外部数据源："
                       "SEC EDGAR (sec.gov/edgar)、TipRanks、Seeking Alpha、Yahoo Finance Analyst、"
                       "FRED 宏观面、Bloomberg/Refinitiv 终端、券商研报聚合 Wallstreetzen / Zacks 等。")}
    # A 股路径：原样
    try:
        code = _code6(symbol)
        df = ak.stock_research_report_em(symbol=code)
        if df is None or len(df) == 0:
            return ok([], meta("akshare.stock_research_report_em", 0)) | {"note": f"{code} 近期无研报"}
        df = df.head(10)
        eps_cols = [c for c in df.columns if "盈利预测" in c]
        keep = ["日期", "机构", "报告名称", "东财评级"] + eps_cols[:4] + ["报告PDF链接"]
        keep = [c for c in keep if c in df.columns]
        records = json_safe(df[keep].to_dict(orient="records"))
        return ok(
            records,
            meta("akshare.stock_research_report_em", len(records)),
            artifact=_table_artifact(f"{code} 券商研报评级（{len(records)} 条）", records),
        )
    except Exception as e:  # noqa: BLE001
        return err(f"研报获取失败: {type(e).__name__}: {e}")


# ------------------------------------------------------ market micro ------


@skill(
    "get_lhb",
    "获取龙虎榜明细（代码/名称/上榜日/收盘价/涨跌幅/净买额/上榜原因/换手率），限30条。",
    {
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "YYYYMMDD，默认7天前"},
            "end_date": {"type": "string", "description": "YYYYMMDD，默认今天"},
        },
        "required": [],
    },
    internal=True,)
def get_lhb(start_date: Optional[str] = None, end_date: Optional[str] = None) -> dict:
    try:
        end8 = norm_date(end_date, date.today())
        start8 = norm_date(start_date, date.today() - timedelta(days=7))
        df = ak.stock_lhb_detail_em(start_date=start8, end_date=end8)
        if df is None or len(df) == 0:
            return ok([], meta("akshare.stock_lhb_detail_em", 0)) | {
                "note": f"{start8}~{end8} 无龙虎榜数据"}
        df = df.head(30)
        keep = ["代码", "名称", "上榜日", "收盘价", "涨跌幅", "龙虎榜净买额", "龙虎榜成交额",
                "换手率", "上榜原因"]
        keep = [c for c in keep if c in df.columns]
        records = json_safe(df[keep].to_dict(orient="records"))
        return ok(
            records,
            meta("akshare.stock_lhb_detail_em", len(records)),
            artifact=_table_artifact(f"龙虎榜（{start8}~{end8}，{len(records)} 条）", records),
        )
    except Exception as e:  # noqa: BLE001
        return err(f"龙虎榜获取失败: {type(e).__name__}: {e}")


@skill(
    "get_margin",
    "获取上交所融资融券汇总（融资余额/融资买入额/融券余量/两融余额 按日）。",
    {
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "YYYYMMDD，默认14天前"},
            "end_date": {"type": "string", "description": "YYYYMMDD，默认今天"},
        },
        "required": [],
    },
    internal=True,)
def get_margin(start_date: Optional[str] = None, end_date: Optional[str] = None) -> dict:
    try:
        end8 = norm_date(end_date, date.today())
        start8 = norm_date(start_date, date.today() - timedelta(days=14))
        df = ak.stock_margin_sse(start_date=start8, end_date=end8)
        if df is None or len(df) == 0:
            return ok([], meta("akshare.stock_margin_sse", 0)) | {
                "note": f"{start8}~{end8} 无融资融券数据"}
        df = df.sort_values("信用交易日期")
        if len(df) > 30:
            df = df.tail(30)
        records = json_safe(df.to_dict(orient="records"))
        return ok(
            records,
            meta("akshare.stock_margin_sse", len(records)),
            artifact=_table_artifact(f"融资融券（{start8}~{end8}）", records),
        )
    except Exception as e:  # noqa: BLE001
        return err(f"融资融券获取失败: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- macro ----


@skill(
    "get_macro",
    "获取宏观指标近24期：cpi/ppi/pmi/gdp/bond_yield（10年期国债收益率）。",
    {
        "type": "object",
        "properties": {
            "indicator": {"type": "string", "enum": ["cpi", "ppi", "pmi", "gdp", "bond_yield"],
                          "description": "指标类型"},
        },
        "required": ["indicator"],
    },
    internal=True,)
def get_macro(indicator: str) -> dict:
    try:
        ind = (indicator or "").strip().lower()
        if ind == "cpi":
            df = ak.macro_china_cpi().tail(24)
            xcol, series = "月份", [("全国-同比增长", "CPI 同比(%)"), ("全国-环比增长", "CPI 环比(%)")]
            source = "akshare.macro_china_cpi"
            title = "中国 CPI（近24月）"
        elif ind == "ppi":
            df = ak.macro_china_ppi().tail(24)
            xcol, series = "月份", [("当月同比增长", "PPI 同比(%)")]
            source = "akshare.macro_china_ppi"
            title = "中国 PPI（近24月）"
        elif ind == "pmi":
            df = ak.macro_china_pmi().tail(24)
            xcol, series = "月份", [("制造业-指数", "制造业PMI"), ("非制造业-指数", "非制造业PMI")]
            source = "akshare.macro_china_pmi"
            title = "中国 PMI（近24月）"
        elif ind == "gdp":
            df = ak.macro_china_gdp().tail(24)
            xcol, series = "季度", [("国内生产总值-同比增长", "GDP 同比(%)")]
            source = "akshare.macro_china_gdp"
            title = "中国 GDP 同比（近24季）"
        elif ind == "bond_yield":
            end8 = date.today().strftime("%Y%m%d")
            df = None
            # akshare 该接口对超长区间返回空，350 天窗口实测可用；失败再退 60 天
            for days in (350, 60):
                start8 = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
                tmp = ak.bond_china_yield(start_date=start8, end_date=end8)
                if tmp is not None and len(tmp) > 0:
                    df = tmp
                    break
            if df is not None and "曲线名称" in df.columns:
                gb = df[df["曲线名称"].astype(str).str.contains("国债")]
                if len(gb) > 0:
                    df = gb
            if df is not None and len(df) > 0:
                df = df.sort_values("日期").tail(24)
            xcol, series = "日期", [("10年", "10年期国债收益率(%)"), ("1年", "1年期国债收益率(%)")]
            source = "akshare.bond_china_yield"
            title = "中债国债收益率（近24期）"
        else:
            return err(f"未知宏观指标: {indicator}（可选 cpi/ppi/pmi/gdp/bond_yield）")
        if df is None or len(df) == 0:
            return err(f"宏观指标 {indicator} 无数据")
        df[xcol] = df[xcol].astype(str)
        records = []
        for _, r in df.iterrows():
            rec = {"期间": r[xcol]}
            for col, label in series:
                if col in df.columns:
                    rec[label] = json_safe(r[col])
            records.append(rec)
        line = {
            "kind": "line",
            "title": title,
            "payload": {
                "x": df[xcol].tolist(),
                "series": [{"name": label, "data": [json_safe(v) for v in df[col].tolist()]}
                           for col, label in series if col in df.columns],
                "yname": "%",
            },
        }
        return ok(records, meta(source, len(records)), artifact=line)
    except Exception as e:  # noqa: BLE001
        return err(f"宏观指标获取失败: {type(e).__name__}: {e}")
