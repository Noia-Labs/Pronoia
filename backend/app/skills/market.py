"""Market data skills: search_stock / get_stock_daily / get_index_daily /
get_sector_spot / get_current_date (design.md §2/§4).

Only whitelisted data sources are used:
  - sina suggest API for code search (NOT akshare)
  - akshare.stock_zh_a_daily (sina) with fallback akshare.stock_zh_a_hist_tx (tencent)
  - akshare.stock_zh_index_daily / akshare.stock_sector_spot
  - akshare.stock_us_daily (东方财富) for US-listed tickers (AAPL/TSLA/...)
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

import akshare as ak
import pandas as pd
import requests
import yfinance as yf

from ..market_runtime import call_sina_history
from .registry import err, meta, ok, skill
from . import cache
from .price_data import PriceFetchError, fetch_price_frame, resolve_security_ref

# ------------------------------------------------------------ helpers ------


# 美股代码：以字母开头、1~5 字符，可含 . / -（如 AAPL / BRK.B / RDS-A）
US_SYMBOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,5}$")


def is_us_symbol(symbol: str) -> bool:
    """判断是否为美股字母代码（AAPL/TSLA/NVDA/BRK.B 等）。纯数字或 sh/sz/bj 前缀视为 A 股。"""
    s = (symbol or "").strip()
    if not s or s.isdigit():
        return False
    return bool(US_SYMBOL_RE.match(s))


def is_a_share_index_symbol(symbol: str) -> bool:
    """判断是否为常见 A 股指数代码（如 sh000300 / sz399001 / sz399006）。"""
    s = (symbol or "").strip().lower()
    return bool(re.match(r"^(sh000\d{3}|sz399\d{3})$", s))


def norm_us_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper()


# 常用美股中文/英文名 → ticker 映射（akshare.get_us_stock_name 网络不稳定时的兜底）
# 键使用全小写；查询时调用方需先 lower 后再查
_US_NAME_MAP: dict[str, tuple[str, str, str]] = {
    # 中文名
    "亚马逊": ("AMZN", "Amazon.com", "纳斯达克"),
    "苹果": ("AAPL", "Apple Inc.", "纳斯达克"),
    "特斯拉": ("TSLA", "Tesla, Inc.", "纳斯达克"),
    "英伟达": ("NVDA", "NVIDIA Corporation", "纳斯达克"),
    "微软": ("MSFT", "Microsoft Corporation", "纳斯达克"),
    "谷歌": ("GOOGL", "Alphabet Inc. (Class A)", "纳斯达克"),
    "alphabet": ("GOOGL", "Alphabet Inc. (Class A)", "纳斯达克"),
    "脸书": ("META", "Meta Platforms, Inc.", "纳斯达克"),
    "meta": ("META", "Meta Platforms, Inc.", "纳斯达克"),
    "奈飞": ("NFLX", "Netflix, Inc.", "纳斯达克"),
    "netflix": ("NFLX", "Netflix, Inc.", "纳斯达克"),
    "英特": ("INTC", "Intel Corporation", "纳斯达克"),
    "intel": ("INTC", "Intel Corporation", "纳斯达克"),
    "波音": ("BA", "The Boeing Company", "纽交所"),
    "boeing": ("BA", "The Boeing Company", "纽交所"),
    "可口可乐": ("KO", "The Coca-Cola Company", "纽交所"),
    "coca-cola": ("KO", "The Coca-Cola Company", "纽交所"),
    "百事": ("PEP", "PepsiCo, Inc.", "纳斯达克"),
    "pepsico": ("PEP", "PepsiCo, Inc.", "纳斯达克"),
    "迪士尼": ("DIS", "The Walt Disney Company", "纽交所"),
    "disney": ("DIS", "The Walt Disney Company", "纽交所"),
    "摩根大通": ("JPM", "JPMorgan Chase & Co.", "纽交所"),
    "jpmorgan": ("JPM", "JPMorgan Chase & Co.", "纽交所"),
    "visa": ("V", "Visa Inc.", "纽交所"),
    "万事达": ("MA", "Mastercard Incorporated", "纽交所"),
    "mastercard": ("MA", "Mastercard Incorporated", "纽交所"),
    "沃尔玛": ("WMT", "Walmart Inc.", "纽交所"),
    "walmart": ("WMT", "Walmart Inc.", "纽交所"),
    "耐克": ("NKE", "NIKE, Inc.", "纽交所"),
    "nike": ("NKE", "NIKE, Inc.", "纽交所"),
    "星巴克": ("SBUX", "Starbucks Corporation", "纳斯达克"),
    "starbucks": ("SBUX", "Starbucks Corporation", "纳斯达克"),
    "宝洁": ("PG", "The Procter & Gamble Company", "纽交所"),
    "强生": ("JNJ", "Johnson & Johnson", "纽交所"),
    "辉瑞": ("PFE", "Pfizer Inc.", "纽交所"),
    "pfizer": ("PFE", "Pfizer Inc.", "纽交所"),
    "中概互联": ("KWEB", "KraneShares CSI China Internet", "纽交所"),
    "阿里巴巴": ("BABA", "Alibaba Group Holding Limited", "纽交所"),
    "alibaba": ("BABA", "Alibaba Group Holding Limited", "纽交所"),
    "京东": ("JD", "JD.com, Inc.", "纳斯达克"),
    "jd.com": ("JD", "JD.com, Inc.", "纳斯达克"),
    "拼多多": ("PDD", "PDD Holdings Inc.", "纳斯达克"),
    "pinduoduo": ("PDD", "PDD Holdings Inc.", "纳斯达克"),
    "pdd": ("PDD", "PDD Holdings Inc.", "纳斯达克"),
    "百度": ("BIDU", "Baidu, Inc.", "纳斯达克"),
    "baidu": ("BIDU", "Baidu, Inc.", "纳斯达克"),
    "蔚来": ("NIO", "NIO Inc.", "纽交所"),
    "nio": ("NIO", "NIO Inc.", "纽交所"),
    "小鹏": ("XPEV", "XPeng Inc.", "纽交所"),
    "xpeng": ("XPEV", "XPeng Inc.", "纽交所"),
    "理想": ("LI", "Li Auto Inc.", "纳斯达克"),
    "li auto": ("LI", "Li Auto Inc.", "纳斯达克"),
    "b站": ("BILI", "Bilibili Inc.", "纳斯达克"),
    "bilibili": ("BILI", "Bilibili Inc.", "纳斯达克"),
    # 常用英文短名（用户可能直接用英文搜）
    "amazon": ("AMZN", "Amazon.com", "纳斯达克"),
    "apple": ("AAPL", "Apple Inc.", "纳斯达克"),
    "tesla": ("TSLA", "Tesla, Inc.", "纳斯达克"),
    "nvidia": ("NVDA", "NVIDIA Corporation", "纳斯达克"),
    "microsoft": ("MSFT", "Microsoft Corporation", "纳斯达克"),
    "google": ("GOOGL", "Alphabet Inc. (Class A)", "纳斯达克"),
    "ibm": ("IBM", "International Business Machines", "纽交所"),
    "cisco": ("CSCO", "Cisco Systems, Inc.", "纳斯达克"),
    "oracle": ("ORCL", "Oracle Corporation", "纽交所"),
    "adobe": ("ADBE", "Adobe Inc.", "纳斯达克"),
    "uber": ("UBER", "Uber Technologies, Inc.", "纽交所"),
    "airbnb": ("ABNB", "Airbnb, Inc.", "纳斯达克"),
    "spotify": ("SPOT", "Spotify Technology S.A.", "纽交所"),
    "shopify": ("SHOP", "Shopify Inc.", "纽交所"),
    # ---- 消费 / 零售 / 餐饮 ----
    "好市多": ("COST", "Costco Wholesale Corporation", "纳斯达克"),
    "costco": ("COST", "Costco Wholesale Corporation", "纳斯达克"),
    "麦当劳": ("MCD", "McDonald's Corporation", "纽交所"),
    "mcdonald": ("MCD", "McDonald's Corporation", "纽交所"),
    "mcd": ("MCD", "McDonald's Corporation", "纽交所"),
    "开市客": ("COST", "Costco Wholesale Corporation", "纳斯达克"),
    "home depot": ("HD", "The Home Depot, Inc.", "纽交所"),
    "家居": ("HD", "The Home Depot, Inc.", "纽交所"),
    "hd": ("HD", "The Home Depot, Inc.", "纽交所"),
    "homedepot": ("HD", "The Home Depot, Inc.", "纽交所"),
    "露露柠檬": ("LULU", "Lululemon Athletica Inc.", "纳斯达克"),
    "lululemon": ("LULU", "Lululemon Athletica Inc.", "纳斯达克"),
    # ---- 金融 / 银行 ----
    "美国银行": ("BAC", "Bank of America Corporation", "纽交所"),
    "bac": ("BAC", "Bank of America Corporation", "纽交所"),
    "富国银行": ("WFC", "Wells Fargo & Company", "纽交所"),
    "wells fargo": ("WFC", "Wells Fargo & Company", "纽交所"),
    "wfc": ("WFC", "Wells Fargo & Company", "纽交所"),
    "花旗": ("C", "Citigroup Inc.", "纽交所"),
    "citigroup": ("C", "Citigroup Inc.", "纽交所"),
    "citi": ("C", "Citigroup Inc.", "纽交所"),
    "高盛": ("GS", "The Goldman Sachs Group, Inc.", "纽交所"),
    "goldman": ("GS", "The Goldman Sachs Group, Inc.", "纽交所"),
    "gs": ("GS", "The Goldman Sachs Group, Inc.", "纽交所"),
    "美国运通": ("AXP", "American Express Company", "纽交所"),
    "amex": ("AXP", "American Express Company", "纽交所"),
    "贝莱德": ("BLK", "BlackRock, Inc.", "纽交所"),
    "blackrock": ("BLK", "BlackRock, Inc.", "纽交所"),
    "伯克希尔": ("BRK.B", "Berkshire Hathaway Inc. (Class B)", "纽交所"),
    "berkshire": ("BRK.B", "Berkshire Hathaway Inc. (Class B)", "纽交所"),
    "brk": ("BRK.B", "Berkshire Hathaway Inc. (Class B)", "纽交所"),
    "黑石": ("BX", "Blackstone Inc.", "纽交所"),
    "blackstone": ("BX", "Blackstone Inc.", "纽交所"),
    "摩根士丹利": ("MS", "Morgan Stanley", "纽交所"),
    "morgan stanley": ("MS", "Morgan Stanley", "纽交所"),
    # ---- 通信 / 媒体 ----
    "at&t": ("T", "AT&T Inc.", "纽交所"),
    "att": ("T", "AT&T Inc.", "纽交所"),
    "verizon": ("VZ", "Verizon Communications Inc.", "纽交所"),
    "威瑞森": ("VZ", "Verizon Communications Inc.", "纽交所"),
    "comcast": ("CMCSA", "Comcast Corporation", "纳斯达克"),
    "康卡斯特": ("CMCSA", "Comcast Corporation", "纳斯达克"),
    "奈飞": ("NFLX", "Netflix, Inc.", "纳斯达克"),
    "netflix": ("NFLX", "Netflix, Inc.", "纳斯达克"),
    # ---- 半导体 / 硬件 ----
    "amd": ("AMD", "Advanced Micro Devices, Inc.", "纳斯达克"),
    "超微": ("AMD", "Advanced Micro Devices, Inc.", "纳斯达克"),
    "高通": ("QCOM", "QUALCOMM Incorporated", "纳斯达克"),
    "qualcomm": ("QCOM", "QUALCOMM Incorporated", "纳斯达克"),
    "德州仪器": ("TXN", "Texas Instruments Incorporated", "纳斯达克"),
    "texas instruments": ("TXN", "Texas Instruments Incorporated", "纳斯达克"),
    "ti": ("TXN", "Texas Instruments Incorporated", "纳斯达克"),
    "asml": ("ASML", "ASML Holding N.V.", "纳斯达克"),
    "阿斯麦": ("ASML", "ASML Holding N.V.", "纳斯达克"),
    "台积电": ("TSM", "Taiwan Semiconductor Manufacturing", "纽交所"),
    "tsmc": ("TSM", "Taiwan Semiconductor Manufacturing", "纽交所"),
    "应用材料": ("AMAT", "Applied Materials, Inc.", "纳斯达克"),
    "applied materials": ("AMAT", "Applied Materials, Inc.", "纳斯达克"),
    "博通": ("AVGO", "Broadcom Inc.", "纳斯达克"),
    "broadcom": ("AVGO", "Broadcom Inc.", "纳斯达克"),
    "lam research": ("LRCX", "Lam Research Corporation", "纳斯达克"),
    # ---- 工业 / 国防 / 能源 ----
    "波音": ("BA", "The Boeing Company", "纽交所"),
    "boeing": ("BA", "The Boeing Company", "纽交所"),
    "卡特彼勒": ("CAT", "Caterpillar Inc.", "纽交所"),
    "caterpillar": ("CAT", "Caterpillar Inc.", "纽交所"),
    "洛克希德": ("LMT", "Lockheed Martin Corporation", "纽交所"),
    "lockheed": ("LMT", "Lockheed Martin Corporation", "纽交所"),
    "雷神": ("RTX", "RTX Corporation", "纽交所"),
    "raytheon": ("RTX", "RTX Corporation", "纽交所"),
    "3m": ("MMM", "3M Company", "纽交所"),
    "通用电气": ("GE", "GE Aerospace", "纽交所"),
    "ge": ("GE", "GE Aerospace", "纽交所"),
    "霍尼韦尔": ("HON", "Honeywell International Inc.", "纳斯达克"),
    "honeywell": ("HON", "Honeywell International Inc.", "纳斯达克"),
    "福特": ("F", "Ford Motor Company", "纽交所"),
    "ford": ("F", "Ford Motor Company", "纽交所"),
    "通用汽车": ("GM", "General Motors Company", "纽交所"),
    "rivian": ("RIVN", "Rivian Automotive, Inc.", "纳斯达克"),
    "lucid": ("LCID", "Lucid Group, Inc.", "纳斯达克"),
    "特斯拉对手": ("RIVN", "Rivian Automotive, Inc.", "纳斯达克"),
    "埃克森": ("XOM", "Exxon Mobil Corporation", "纽交所"),
    "exxon": ("XOM", "Exxon Mobil Corporation", "纽交所"),
    "雪佛龙": ("CVX", "Chevron Corporation", "纽交所"),
    "chevron": ("CVX", "Chevron Corporation", "纽交所"),
    # ---- 医疗 / 医药 ----
    "辉瑞": ("PFE", "Pfizer Inc.", "纽交所"),
    "pfizer": ("PFE", "Pfizer Inc.", "纽交所"),
    "强生": ("JNJ", "Johnson & Johnson", "纽交所"),
    "默克": ("MRK", "Merck & Co., Inc.", "纽交所"),
    "merck": ("MRK", "Merck & Co., Inc.", "纽交所"),
    "礼来": ("LLY", "Eli Lilly and Company", "纽交所"),
    "eli lilly": ("LLY", "Eli Lilly and Company", "纽交所"),
    "lilly": ("LLY", "Eli Lilly and Company", "纽交所"),
    "艾伯维": ("ABBV", "AbbVie Inc.", "纽交所"),
    "abbvie": ("ABBV", "AbbVie Inc.", "纽交所"),
    "百时美": ("BMY", "Bristol-Myers Squibb Company", "纽交所"),
    "bms": ("BMY", "Bristol-Myers Squibb Company", "纽交所"),
    "bristol-myers squibb": ("BMY", "Bristol-Myers Squibb Company", "纽交所"),
    "medtronic": ("MDT", "Medtronic plc", "纽交所"),
    "mdt": ("MDT", "Medtronic plc", "纽交所"),
    "雅培": ("ABT", "Abbott Laboratories", "纽交所"),
    "abbott": ("ABT", "Abbott Laboratories", "纽交所"),
    "联合健康": ("UNH", "UnitedHealth Group Incorporated", "纽交所"),
    "unitedhealth": ("UNH", "UnitedHealth Group Incorporated", "纽交所"),
    "moderna": ("MRNA", "Moderna, Inc.", "纳斯达克"),
    "莫德纳": ("MRNA", "Moderna, Inc.", "纳斯达克"),
    "吉利德": ("GILD", "Gilead Sciences, Inc.", "纳斯达克"),
    "gilead": ("GILD", "Gilead Sciences, Inc.", "纳斯达克"),
    "再生元": ("REGN", "Regeneron Pharmaceuticals, Inc.", "纳斯达克"),
    "regeneron": ("REGN", "Regeneron Pharmaceuticals, Inc.", "纳斯达克"),
    # ---- 科技 / SaaS / 互联网 / 加密 ----
    "salesforce": ("CRM", "Salesforce, Inc.", "纽交所"),
    "赛富时": ("CRM", "Salesforce, Inc.", "纽交所"),
    "crm": ("CRM", "Salesforce, Inc.", "纽交所"),
    "snowflake": ("SNOW", "Snowflake Inc.", "纽交所"),
    "雪花": ("SNOW", "Snowflake Inc.", "纽交所"),
    "palantir": ("PLTR", "Palantir Technologies Inc.", "纳斯达克"),
    "pltr": ("PLTR", "Palantir Technologies Inc.", "纳斯达克"),
    "coinbase": ("COIN", "Coinbase Global, Inc.", "纳斯达克"),
    "coin": ("COIN", "Coinbase Global, Inc.", "纳斯达克"),
    "paypal": ("PYPL", "PayPal Holdings, Inc.", "纳斯达克"),
    "贝宝": ("PYPL", "PayPal Holdings, Inc.", "纳斯达克"),
    "block": ("SQ", "Block, Inc.", "纽交所"),
    "sq": ("SQ", "Block, Inc.", "纽交所"),
    "roblox": ("RBLX", "Roblox Corporation", "纽交所"),
    "unity": ("U", "Unity Software Inc.", "纽交所"),
    "doordash": ("DASH", "DoorDash, Inc.", "纳斯达克"),
    "dashi": ("DASH", "DoorDash, Inc.", "纳斯达克"),
    "etsy": ("ETSY", "Etsy, Inc.", "纳斯达克"),
    "snap": ("SNAP", "Snap Inc.", "纽交所"),
    "snapchat": ("SNAP", "Snap Inc.", "纽交所"),
    "pinterest": ("PINS", "Pinterest, Inc.", "纽交所"),
    "roku": ("ROKU", "Roku, Inc.", "纳斯达克"),
    "lyft": ("LYFT", "Lyft, Inc.", "纳斯达克"),
    "zoom": ("ZM", "Zoom Video Communications, Inc.", "纳斯达克"),
    "docu": ("DOCU", "DocuSign, Inc.", "纳斯达克"),
    "docusign": ("DOCU", "DocuSign, Inc.", "纳斯达克"),
    # ---- 出行 / 旅游 / 航司 ----
    "达美": ("DAL", "Delta Air Lines, Inc.", "纽交所"),
    "delta": ("DAL", "Delta Air Lines, Inc.", "纽交所"),
    "美国航空": ("AAL", "American Airlines Group Inc.", "纳斯达克"),
    "american airlines": ("AAL", "American Airlines Group Inc.", "纳斯达克"),
    "西南航空": ("LUV", "Southwest Airlines Co.", "纽交所"),
    "southwest": ("LUV", "Southwest Airlines Co.", "纽交所"),
    "联合航空": ("UAL", "United Airlines Holdings, Inc.", "纳斯达克"),
    "united airlines": ("UAL", "United Airlines Holdings, Inc.", "纳斯达克"),
    "万豪": ("MAR", "Marriott International, Inc.", "纳斯达克"),
    "marriott": ("MAR", "Marriott International, Inc.", "纳斯达克"),
    "希尔顿": ("HLT", "Hilton Worldwide Holdings Inc.", "纽交所"),
    "hilton": ("HLT", "Hilton Worldwide Holdings Inc.", "纽交所"),
    "booking": ("BKNG", "Booking Holdings Inc.", "纳斯达克"),
    "expedia": ("EXPE", "Expedia Group, Inc.", "纳斯达克"),
    # ---- 汽车 / 出行 ----
    "法拉利": ("RACE", "Ferrari N.V.", "纽交所"),
    "ferrari": ("RACE", "Ferrari N.V.", "纽交所"),
    "丰田": ("TM", "Toyota Motor Corporation", "纽交所"),
    "toyota": ("TM", "Toyota Motor Corporation", "纽交所"),
    "本田": ("HMC", "Honda Motor Co., Ltd.", "纽交所"),
    "honda": ("HMC", "Honda Motor Co., Ltd.", "纽交所"),
    "加拿大皇家": ("RY", "Royal Bank of Canada", "纽交所"),
    "rbc": ("RY", "Royal Bank of Canada", "纽交所"),
    # ---- 中概股补全 ----
    "网易": ("NTES", "NetEase, Inc.", "纳斯达克"),
    "netease": ("NTES", "NetEase, Inc.", "纳斯达克"),
    "腾讯音乐": ("TME", "Tencent Music Entertainment", "纽交所"),
    "中通": ("ZTO", "ZTO Express (Cayman) Inc.", "纽交所"),
    "zto": ("ZTO", "ZTO Express (Cayman) Inc.", "纽交所"),
    "百胜中国": ("YUMC", "Yum China Holdings, Inc.", "纽交所"),
    "yum china": ("YUMC", "Yum China Holdings, Inc.", "纽交所"),
    "好未来": ("TAL", "TAL Education Group", "纽交所"),
    "tal": ("TAL", "TAL Education Group", "纽交所"),
    "新东方": ("EDU", "New Oriental Education & Technology", "纽交所"),
    "新东方教育": ("EDU", "New Oriental Education & Technology", "纽交所"),
    "唯品会": ("VIPS", "Vipshop Holdings Limited", "纽交所"),
    "vipshop": ("VIPS", "Vipshop Holdings Limited", "纽交所"),
    # ---- 指数 ETF ----
    "spy": ("SPY", "SPDR S&P 500 ETF Trust", "纽交所"),
    "标普500": ("SPY", "SPDR S&P 500 ETF Trust", "纽交所"),
    "标普 500": ("SPY", "SPDR S&P 500 ETF Trust", "纽交所"),
    "s&p 500": ("SPY", "SPDR S&P 500 ETF Trust", "纽交所"),
    "s&p500": ("SPY", "SPDR S&P 500 ETF Trust", "纽交所"),
    "qqq": ("QQQ", "Invesco QQQ Trust", "纳斯达克"),
    "纳斯达克100": ("QQQ", "Invesco QQQ Trust", "纳斯达克"),
    "nasdaq 100": ("QQQ", "Invesco QQQ Trust", "纳斯达克"),
    "tqqq": ("TQQQ", "ProShares UltraPro QQQ", "纳斯达克"),
    "三倍做多纳斯达克": ("TQQQ", "ProShares UltraPro QQQ", "纳斯达克"),
    "sqqq": ("SQQQ", "ProShares UltraPro Short QQQ", "纳斯达克"),
    "三倍做空纳斯达克": ("SQQQ", "ProShares UltraPro Short QQQ", "纳斯达克"),
    "voo": ("VOO", "Vanguard S&P 500 ETF", "纽交所"),
    "vti": ("VTI", "Vanguard Total Stock Market ETF", "纽交所"),
    "iwm": ("IWM", "iShares Russell 2000 ETF", "纽交所"),
    "罗素2000": ("IWM", "iShares Russell 2000 ETF", "纽交所"),
    "dia": ("DIA", "SPDR Dow Jones Industrial Average ETF", "纽交所"),
    "道琼斯etf": ("DIA", "SPDR Dow Jones Industrial Average ETF", "纽交所"),
    "arkk": ("ARKK", "ARK Innovation ETF", "纽交所"),
    "木头姐": ("ARKK", "ARK Innovation ETF", "纽交所"),
    "kweb": ("KWEB", "KraneShares CSI China Internet ETF", "纽交所"),
    "中概互联etf": ("KWEB", "KraneShares CSI China Internet ETF", "纽交所"),
    "mchi": ("MCHI", "iShares MSCI China ETF", "纳斯达克"),
    "msci中国": ("MCHI", "iShares MSCI China ETF", "纳斯达克"),
    "xlre": ("XLRE", "The Real Estate Select Sector SPDR Fund", "纽交所"),
    "kbe": ("KBE", "SPDR S&P Bank ETF", "纽交所"),
}


# 所有已知 ticker 集合（从 _US_NAME_MAP 的 value[0] 提取），用于「纯 ticker 字符串」直接识别
_US_TICKER_SET: frozenset[str] = frozenset(v[0].upper() for v in _US_NAME_MAP.values())


def _lookup_us_name(keyword: str) -> list[dict]:
    """本地兜底：把 keyword 解释为美股代码。返回最多 3 个匹配。"""
    if not keyword:
        return []
    k = keyword.strip().lower()
    if not k:
        return []
    # 0) 提取 ticker token 直接命中（处理 "NVDA" / "NVDA 英伟达" / "英伟达 NVDA" 等混合串）
    #    _US_NAME_MAP 的 key 是英文/中文名，不含 ticker 本身；用 _US_TICKER_SET 反查 value
    for token in re.findall(r"[A-Za-z]{1,5}", keyword):
        upper = token.upper()
        if upper in _US_TICKER_SET:
            for v in _US_NAME_MAP.values():
                if v[0].upper() == upper:
                    return [{"name": v[1], "code": v[0], "symbol": v[0], "market": "美股", "exchange": v[2]}]
    # 1) 精确匹配（中文名 / 英文小写名）
    hit = _US_NAME_MAP.get(k)
    if hit:
        return [{"name": hit[1], "code": hit[0], "symbol": hit[0], "market": "美股", "exchange": hit[2]}]
    # 2) 子串匹配（中文名 / ticker 前缀）
    out: list[dict] = []
    seen: set[str] = set()
    for key, val in _US_NAME_MAP.items():
        if key in seen:
            continue
        if k in key or key.startswith(k):
            out.append({"name": val[1], "code": val[0], "symbol": val[0], "market": "美股", "exchange": val[2]})
            seen.add(val[0])
        if len(out) >= 3:
            break
    return out


def norm_symbol(symbol: str) -> str:
    """Normalize user/LLM supplied code to sina format, e.g. sh600519 / sz300750."""
    s = (symbol or "").strip().lower()
    if not s:
        raise ValueError("股票代码为空")
    m = re.match(r"^(sh|sz|bj)(\d{6})$", s)
    if m:
        return m.group(1) + m.group(2)
    m = re.match(r"^(\d{6})\.(sh|sz|bj)$", s)
    if m:
        return m.group(2) + m.group(1)
    m = re.search(r"(\d{6})", s)
    if m:
        code = m.group(1)
        # 沪市 ETF / LOF 常见代码段为 50/51/52/56/58。此前所有 5 开头代码
        # 都落到 sz，导致 SH516160 等被请求为 sz516160，Sina/Tencent 均失败。
        if code.startswith(("50", "51", "52", "56", "58")):
            return "sh" + code
        if code[0] in ("6", "9"):
            return "sh" + code
        if code[0] in ("4", "8"):
            return "bj" + code
        return "sz" + code
    raise ValueError(f"无法识别股票代码: {symbol}")


def norm_index_symbol(symbol: str) -> str:
    s = (symbol or "").strip().lower()
    if re.match(r"^(sh|sz)\d{6}$", s):
        return s
    m = re.search(r"(\d{6})", s)
    if m:
        code = m.group(1)
        return ("sh" if code.startswith(("0", "9")) else "sz") + code
    raise ValueError(f"无法识别指数代码: {symbol}")


def norm_date(s: Optional[str], default: Optional[date] = None) -> str:
    """Accept YYYYMMDD / YYYY-MM-DD / ISO datetime -> YYYYMMDD."""
    if not s:
        d = default or date.today()
        return d.strftime("%Y%m%d")
    s = str(s).strip()
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", s)
    if m:
        return "".join(m.groups())
    raise ValueError(f"无法识别日期: {s}")


def _d8_to_iso(d8: str) -> str:
    return f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"


def _clean_ohlcv(df: pd.DataFrame, limit: int = 250) -> tuple[pd.DataFrame, bool]:
    """Normalize a daily-kline df to date/open/close/high/low/volume sorted asc."""
    col_map = {}
    for c in df.columns:
        cl = str(c).lower()
        if cl in ("date", "日期"):
            col_map[c] = "date"
        elif cl in ("open", "开盘"):
            col_map[c] = "open"
        elif cl in ("close", "收盘"):
            col_map[c] = "close"
        elif cl in ("high", "最高"):
            col_map[c] = "high"
        elif cl in ("low", "最低"):
            col_map[c] = "low"
        elif cl in ("volume", "成交量"):
            col_map[c] = "volume"
    df = df.rename(columns=col_map)
    keep = [c for c in ("date", "open", "close", "high", "low", "volume") if c in df.columns]
    df = df[keep].copy()
    if "volume" not in df.columns:
        df["volume"] = 0
    df["date"] = df["date"].astype(str).str[:10]
    for c in ("open", "close", "high", "low", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    truncated = False
    if len(df) > limit:
        df = df.tail(limit).reset_index(drop=True)
        truncated = True
    return df, truncated


# ------------------------------------------------------------- skills ------


@skill(
    "search_stock",
    "按名称/拼音/代码搜索股票，返回前5个匹配（名称、代码、symbol）。"
    "A 股用 sina suggest；本地美股名称映射兜底（约 100+ 个常见标的，覆盖消费/金融/科技/中概/ETF）。"
    "两路都查，结果去重合并（market 字段区分美股/ A 股）。",
    {
        "type": "object",
        "properties": {"keyword": {"type": "string", "description": "股票名称、简称或代码片段"}},
        "required": ["keyword"],
    },
    internal=False,)
@cache.cached("search")
def search_stock(keyword: str) -> dict:
    # 1) A 股搜索：sina suggest3（网络失败也不算致命）
    a_items: list[dict] = []
    a_meta: dict = {"source": "sina.suggest3", "count": 0}
    try:
        resp = requests.get(
            "https://suggest3.sinajs.cn/suggest/type=11,12&key={}&name=suggestdata".format(
                requests.utils.quote(keyword)
            ),
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
            timeout=10,
        )
        resp.encoding = "gbk"
        text = resp.text
        m = re.search(r'"(.*)"', text)
        body = m.group(1) if m else ""
        for entry in body.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(",")
            if len(parts) >= 4 and parts[1] in ("11", "12") and re.match(r"^\d{6}$", parts[2]):
                a_items.append({
                    "name": parts[0], "code": parts[2], "symbol": parts[3],
                    "market": "A股", "exchange": "上交所/深交所/北交所",
                })
                if len(a_items) >= 5:
                    break
        a_meta["count"] = len(a_items)
    except Exception as e:  # noqa: BLE001
        a_meta = {"source": "sina.suggest3", "count": 0, "a_share_error": f"{type(e).__name__}: {e}"}

    # 2) 美股兜底：本地常见名称映射（独立于 A 股结果）
    us_items = _lookup_us_name(keyword)

    # 3) 合并去重：ticker 强信号排第一（避免 "NVDA 英伟达" 被 A 股模糊匹配抢走），
    #    其余 A 股优先（与既有行为一致）
    # 3.1 检测 keyword 中是否含已知美股 ticker（_US_TICKER_SET 来自 _US_NAME_MAP 的 value[0]）
    ticker_hint: str | None = None
    for token in re.findall(r"[A-Za-z]{1,5}", keyword or ""):
        t = token.upper()
        if t in _US_TICKER_SET:
            ticker_hint = t
            break
    merged: list[dict] = []
    seen_codes: set[str] = set()
    # 3.2 美股 ticker 精确匹配优先
    if ticker_hint:
        for u in us_items:
            if str(u.get("code", "")).upper() == ticker_hint:
                merged.append(u)
                seen_codes.add(str(u.get("code", "")))
                break
    # 3.3 A 股
    for a in a_items:
        if a.get("code") in seen_codes:
            continue
        merged.append(a)
        seen_codes.add(a.get("code", ""))
    # 3.4 其他美股
    for u in us_items:
        if str(u.get("code", "")) in seen_codes:
            continue
        merged.append(u)
        seen_codes.add(str(u.get("code", "")))
    # 单源总条数 ≤ 5（与原接口约束一致）
    if len(merged) > 5:
        merged = merged[:5]

    if not merged:
        return ok(
            [],
            {"source": "sina.suggest3+us.name_map", "count": 0, "fallback_tried": True},
            artifact=None,
        ) | {"note": f"未找到匹配股票：{keyword}（A 股 / 美股均未命中）"}

    # 拼 meta：标注两条线都跑了
    m_out = {
        "source": "sina.suggest3+us.name_map",
        "count": len(merged),
        "a_share_count": len(a_items),
        "us_count": len(us_items),
    }
    if a_meta.get("a_share_error"):
        m_out["a_share_error"] = a_meta["a_share_error"]
    return ok(merged, m_out)


@skill(
    "resolve_security",
    "统一解析证券代码。显式 symbol 优先且完全本地解析；只有缺少 symbol 时才按 keyword 搜索。"
    "支持 A 股/ETF/指数的 sh/sz/bj 前缀、6 位代码和美股 ticker。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "明确证券代码（优先）"},
            "keyword": {"type": "string", "description": "公司名或代码关键词（symbol 缺省时使用）"},
            "market": {"type": "string", "description": "可选市场提示：CN/US/A股/美股"},
        },
        "required": [],
        "anyOf": [{"required": ["symbol"]}, {"required": ["keyword"]}],
        "additionalProperties": False,
    },
    internal=True,
)
@cache.cached("search")
def resolve_security(symbol: str | None = None, keyword: str | None = None,
                     market: str | None = None) -> dict:
    """Resolve canonical identifiers before considering a network lookup."""
    raw_symbol = str(symbol or "").strip()
    raw_keyword = str(keyword or "").strip()
    candidate = raw_symbol or raw_keyword
    if not candidate:
        return err("必须提供 symbol 或 keyword")

    # Explicit symbols, numeric codes and uppercase US tickers never need a
    # remote name lookup.  This is the common backtest path because packets
    # already carry a canonical symbol.
    local_candidate = raw_symbol
    if not local_candidate:
        if re.fullmatch(r"(?i)(?:sh|sz|bj)?\d{6}(?:\.(?:ss|sz|bj))?", raw_keyword):
            local_candidate = raw_keyword
        elif raw_keyword == raw_keyword.upper() and US_SYMBOL_RE.fullmatch(raw_keyword):
            local_candidate = raw_keyword
    if local_candidate:
        try:
            ref = resolve_security_ref(local_candidate, market)
        except ValueError as exc:
            return err(str(exc))
        data = ref.as_dict()
        data["name"] = raw_keyword or ref.symbol
        data["resolution_source"] = "explicit_symbol"
        return ok(data, meta("local.resolve_security", 1))

    # Name-only interactive requests retain the existing Sina/local-map path.
    searched = search_stock(raw_keyword)
    matches = searched.get("data") if isinstance(searched, dict) else None
    if not searched.get("ok") or not isinstance(matches, list) or not matches:
        return err(f"未找到股票: {raw_keyword}")
    primary = matches[0]
    found_symbol = str(primary.get("symbol") or primary.get("code") or "").strip()
    found_market = str(primary.get("market") or market or "").strip()
    try:
        ref = resolve_security_ref(found_symbol, found_market)
    except ValueError as exc:
        return err(str(exc))
    data = ref.as_dict()
    data["name"] = str(primary.get("name") or primary.get("名称") or raw_keyword)
    data["resolution_source"] = "name_search"
    data["matches"] = matches[:3]
    return ok(data, meta("search_stock", len(matches)))


@skill(
    "get_stock_daily",
    "获取个股日K线（OHLC+成交量），返回最近至多250个交易日。自动识别市场：A 股 600519 / sh600519；美股字母代码 AAPL/TSLA/NVDA 等。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "股票代码：A 股如 600519 / sh600519；美股如 AAPL"},
            "start_date": {"type": "string", "description": "YYYYMMDD 或 YYYY-MM-DD，美股忽略（接口返回全历史）"},
            "end_date": {"type": "string", "description": "YYYYMMDD 或 YYYY-MM-DD，美股忽略"},
            "adjust": {"type": "string", "enum": ["qfq", "hfq", ""], "description": "复权方式，默认 qfq"},
        },
        "required": ["symbol"],
    },
    internal=True,)
@cache.cached("kline")
def get_stock_daily(symbol: str, start_date: Optional[str] = None,
                    end_date: Optional[str] = None, adjust: str = "qfq") -> dict:
    try:
        end8 = norm_date(end_date, date.today())
        start8 = norm_date(start_date, date.today() - timedelta(days=365))
        fetched = fetch_price_frame(symbol, start8, end8, adjust=adjust)
        sym = fetched.security.symbol
        df, truncated = _clean_ohlcv(fetched.frame, limit=250)
        records = df.to_dict(orient="records")
        artifact = {
            "kind": "kline",
            "title": f"{sym.upper()} 日K线（{_d8_to_iso(start8)}~{_d8_to_iso(end8)}）",
            "payload": {
                "symbol": sym,
                "market": fetched.security.market,
                "security_kind": fetched.security.kind,
                "dates": df["date"].tolist(),
                "ohlc": [[r["open"], r["close"], r["low"], r["high"]] for r in records],
                "volumes": [r["volume"] for r in records],
            },
        }
        m = meta(fetched.provider, len(df))
        m["market"] = fetched.security.market
        m["security_kind"] = fetched.security.kind
        m["provider_attempts"] = list(fetched.attempts)
        if truncated:
            m["note"] = "仅保留最近 250 行"
        return ok(records, m, artifact=artifact)
    except PriceFetchError as e:
        return err(str(e))
    except Exception as e:  # noqa: BLE001
        return err(f"日K获取失败: {type(e).__name__}: {e}")


@skill(
    "get_us_stock_daily",
    "获取美股个股日K线（OHLC+成交量），symbol 为字母代码如 AAPL/TSLA/NVDA/BRK.B。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "美股代码，如 AAPL / TSLA"},
            "adjust": {"type": "string", "enum": ["qfq", "hfq", ""], "description": "复权方式，默认 qfq"},
        },
        "required": ["symbol"],
    },
    internal=True,)
@cache.cached("us_stock")
def get_us_stock_daily(symbol: str, start_date: Optional[str] = None,
                       end_date: Optional[str] = None, adjust: str = "qfq") -> dict:
    """美股日K；共享路由优先 yfinance 单标的，AkShare 作为兜底。"""
    return get_stock_daily(symbol, start_date, end_date, adjust)


@skill(
    "get_index_daily",
    "获取指数日K收盘价序列，symbol 支持 sh000001(上证)/sh000300(沪深300)/sz399001(深成指)/sz399006(创业板指)。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "指数代码，如 sh000300"},
            "start_date": {"type": "string", "description": "默认一年前"},
            "end_date": {"type": "string", "description": "默认今天"},
        },
        "required": ["symbol"],
    },
    internal=True,)
@cache.cached("kline")
def get_index_daily(symbol: str, start_date: Optional[str] = None,
                    end_date: Optional[str] = None) -> dict:
    result = get_stock_daily(symbol, start_date, end_date, adjust="")
    if not result.get("ok"):
        return result
    artifact = result.get("artifact")
    if isinstance(artifact, dict):
        artifact = dict(artifact)
        artifact["kind"] = "line"
        result["artifact"] = artifact
    return result


@skill(
    "get_sector_spot",
    "获取新浪行业板块实时快照（板块、公司数、平均价、涨跌幅、总成交额、领涨股）。",
    {"type": "object", "properties": {}, "required": []},
    internal=True,)
@cache.cached("sector_spot")
def get_sector_spot() -> dict:
    try:
        df = ak.stock_sector_spot(indicator="新浪行业")
        if df is None or len(df) == 0:
            return err("行业板块快照为空")
        df = df.sort_values("涨跌幅", ascending=False).head(40)
        records = []
        for _, r in df.iterrows():
            records.append({
                "板块": r.get("板块"),
                "公司家数": r.get("公司家数"),
                "平均价格": r.get("平均价格"),
                "涨跌幅(%)": r.get("涨跌幅"),
                "总成交额": r.get("总成交额"),
                "领涨股": r.get("股票名称"),
                "领涨股涨跌幅(%)": r.get("个股-涨跌幅"),
            })
        artifact = {
            "kind": "table",
            "title": "新浪行业板块快照（按涨跌幅排序，前40）",
            "payload": {
                "columns": list(records[0].keys()),
                "rows": [[rec[c] for c in rec] for rec in records],
            },
        }
        return ok(records, meta("akshare.stock_sector_spot(新浪行业)", len(records)), artifact=artifact)
    except Exception as e:  # noqa: BLE001
        return err(f"行业板块快照获取失败: {type(e).__name__}: {e}")


@skill(
    "get_us_stock_spot",
    "美股实时行情（akshare.stock_us_spot_em 东方财富源，延迟 15 min）。"
    "symbol 为字母代码如 NVDA/AAPL/TSLA。返回最新价/涨跌幅/总市值/市盈率等。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "美股代码，如 NVDA"},
        },
        "required": ["symbol"],
    },
    internal=True,)
def get_us_stock_spot(symbol: str) -> dict:
    """美股实时行情：先尝试东财源（延迟 15 分钟，含总市值/PE），失败回退新浪源。"""
    sym = norm_us_symbol(symbol)
    sources: list[tuple[str, Any]] = [
        ("akshare.stock_us_spot_em", lambda: ak.stock_us_spot_em()),
        ("akshare.stock_us_spot", lambda: ak.stock_us_spot()),
    ]
    for source, fn in sources:
        try:
            df = fn()
        except Exception as e:  # noqa: BLE001
            continue
        if df is None or len(df) == 0:
            continue
        # 东财源：代码列是 "105.NVDA" 形式；简称列就是 ticker
        mask = None
        for col in ("代码", "代码\uff05", "symbol", "Symbol"):
            if col in df.columns:
                cand = df[col].astype(str)
                mask = (cand == f"105.{sym}") | (cand == f"106.{sym}") | (cand == f"107.{sym}") | (cand.str.upper() == sym)
                if mask.any():
                    break
                mask = None
        if mask is None:
            # 退而求其次：在所有列里找 ticker
            for col in df.columns:
                if df[col].dtype == object:
                    if df[col].astype(str).str.upper().str.contains(f"^{sym}$", regex=True, na=False).any():
                        mask = df[col].astype(str).str.upper() == sym
                        break
        if mask is None or not mask.any():
            continue
        row = df[mask].iloc[0]
        record = {col: (None if pd.isna(row[col]) else row[col]) for col in df.columns}
        return ok({"symbol": sym, **record}, meta(source, 1))
    return err(f"美股 {sym} 实时行情未获取到（东财/新浪源均失败或未找到代码）")


@skill(
    "get_us_stock_info",
    "美股个股基本信息（akshare.stock_individual_basic_info_us_xq 雪球源）。"
    "symbol 为字母代码如 NVDA/AAPL/TSLA。返回公司中英文名、行业、上市日期、简介等。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "美股代码，如 NVDA"},
        },
        "required": ["symbol"],
    },
    internal=True,)
def get_us_stock_info(symbol: str) -> dict:
    """美股个股基本信息（雪球）。"""
    sym = norm_us_symbol(symbol)
    try:
        df = ak.stock_individual_basic_info_us_xq(symbol=sym)
    except Exception as e:  # noqa: BLE001
        return err(f"美股 {sym} 基本信息获取失败: {type(e).__name__}: {e}")
    if df is None or len(df) == 0:
        return err(f"美股 {sym} 基本信息为空")
    # 雪球接口返回两列：item / value，转换成 dict
    try:
        item_col = next((c for c in df.columns if str(c).lower() in ("item", "key", "项目", "名称")), df.columns[0])
        value_col = next((c for c in df.columns if c != item_col), df.columns[-1])
        record: dict = {}
        for _, r in df.iterrows():
            k = str(r[item_col]).strip()
            v = r[value_col]
            if pd.isna(v):
                continue
            record[k] = str(v).strip() if not isinstance(v, (int, float)) else v
    except Exception:
        record = {str(c): df[c].tolist() for c in df.columns}
    return ok({"symbol": sym, **record}, meta("akshare.stock_individual_basic_info_us_xq", len(df)))


@skill(
    "get_us_stock_finance",
    "美股三大财务报表（akshare.stock_financial_us_report_em 东方财富源）。"
    "symbol 为字母代码，report_type 支持 资产负债表/综合损益表/现金流量表，indicator 支持 年报/单季报/累计季报。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "美股代码，如 NVDA"},
            "report_type": {"type": "string", "enum": ["资产负债表", "综合损益表", "现金流量表"],
                            "description": "报表类型，默认 资产负债表"},
            "indicator": {"type": "string", "enum": ["年报", "单季报", "累计季报"],
                          "description": "时间口径，默认 年报"},
        },
        "required": ["symbol"],
    },
    internal=True,)
def get_us_stock_finance(symbol: str, report_type: str = "资产负债表", indicator: str = "年报") -> dict:
    """美股三大报表（资产负债表/综合损益表/现金流量表）。"""
    sym = norm_us_symbol(symbol)
    try:
        df = ak.stock_financial_us_report_em(stock=sym, symbol=report_type, indicator=indicator)
    except Exception as e:  # noqa: BLE001
        return err(f"美股 {sym} {report_type} 获取失败: {type(e).__name__}: {e}")
    if df is None or len(df) == 0:
        return err(f"美股 {sym} {report_type} 为空")
    # 整理为 [{date, type, item, amount}, ...] 列表（按 REPORT_DATE 倒序）
    keep_cols = [c for c in ("REPORT_DATE", "REPORT_TYPE", "ITEM_NAME", "AMOUNT") if c in df.columns]
    if not keep_cols:
        return err(f"美股 {sym} {report_type} 返回结构异常: {list(df.columns)}")
    sub = df[keep_cols].copy()
    sub.columns = ["date", "report", "item", "amount"][:len(keep_cols)]
    sub = sub.head(80)  # 截断防爆
    records = sub.to_dict(orient="records")
    return ok(
        {"symbol": sym, "report_type": report_type, "indicator": indicator, "rows": records},
        meta("akshare.stock_financial_us_report_em", len(records)),
    )


@skill(
    "get_us_stock_indicator",
    "美股财务分析指标（akshare.stock_financial_us_analysis_indicator_em 东方财富源）："
    "营收、净利润、ROE、ROA、负债率、EPS 等。symbol 为字母代码，indicator 支持 年报/单季报/累计季报。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "美股代码，如 NVDA"},
            "indicator": {"type": "string", "enum": ["年报", "单季报", "累计季报"],
                          "description": "时间口径，默认 年报"},
        },
        "required": ["symbol"],
    },
    internal=True,)
def get_us_stock_indicator(symbol: str, indicator: str = "年报") -> dict:
    """美股财务分析指标（营收/净利润/ROE/ROA/负债率/EPS 等）。"""
    sym = norm_us_symbol(symbol)
    try:
        df = ak.stock_financial_us_analysis_indicator_em(symbol=sym, indicator=indicator)
    except Exception as e:  # noqa: BLE001
        return err(f"美股 {sym} 财务指标获取失败: {type(e).__name__}: {e}")
    if df is None or len(df) == 0:
        return err(f"美股 {sym} 财务指标为空")
    # 字段映射：营收 OPERATE_INCOME、净利润 PARENT_HOLDER_NETPROFIT、EPS BASIC_EPS、
    #          ROE 均值 ROE_AVG、ROA、负债率 DEBT_ASSET_RATIO、毛利率/净利率/流动比/速动比
    records = []
    for _, r in df.iterrows():
        item = {
            "date": str(r.get("REPORT_DATE") or r.get("STD_REPORT_DATE") or ""),
            "revenue": r.get("OPERATE_INCOME"),
            "revenue_yoy": r.get("OPERATE_INCOME_YOY"),
            "gross_profit": r.get("GROSS_PROFIT"),
            "net_profit": r.get("PARENT_HOLDER_NETPROFIT"),
            "net_profit_yoy": r.get("PARENT_HOLDER_NETPROFIT_YOY"),
            "eps": r.get("BASIC_EPS"),
            "gross_margin": r.get("GROSS_PROFIT_RATIO"),
            "net_margin": r.get("NET_PROFIT_RATIO"),
            "roe": r.get("ROE_AVG"),
            "roa": r.get("ROA"),
            "debt_ratio": r.get("DEBT_ASSET_RATIO"),
            "current_ratio": r.get("CURRENT_RATIO"),
            "quick_ratio": r.get("SPEED_RATIO"),
            "currency": r.get("CURRENCY_ABBR") or r.get("CURRENCY"),
        }
        records.append({k: (None if (v is None or (isinstance(v, float) and pd.isna(v))) else v) for k, v in item.items()})
    return ok(
        {"symbol": sym, "indicator": indicator, "rows": records},
        meta("akshare.stock_financial_us_analysis_indicator_em", len(records)),
    )


@skill(
    "get_us_stock_news",
    "美股个股新闻（yfinance.Ticker.news Yahoo Finance 源）：标题/摘要/发布时间/来源/原文链接/缩略图。"
    "symbol 为字母代码如 NVDA/AAPL/TSLA。count 控制条数（默认 20，上限 50）。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "美股代码，如 NVDA"},
            "count": {"type": "integer", "description": "返回条数，默认 20，上限 50"},
        },
        "required": ["symbol"],
    },
    internal=True,)
def get_us_stock_news(symbol: str, count: int = 20) -> dict:
    """美股个股新闻（Yahoo Finance via yfinance）。"""
    sym = norm_us_symbol(symbol)
    n = max(1, min(int(count or 20), 50))
    try:
        raw = yf.Ticker(sym).news or []
    except Exception as e:  # noqa: BLE001
        return err(f"美股 {sym} 新闻获取失败: {type(e).__name__}: {e}")
    if not raw:
        return err(f"美股 {sym} 暂无新闻")
    records = []
    for item in raw[:n]:
        c = item.get("content", {}) if isinstance(item, dict) else {}
        if not c and isinstance(item, dict):
            # 兼容老 schema（content 字段平铺在 item 上）
            c = item
        thumb = c.get("thumbnail") or {}
        thumb_url = ""
        if isinstance(thumb, dict):
            for res in thumb.get("resolutions") or []:
                if isinstance(res, dict) and res.get("tag") == "original" and res.get("url"):
                    thumb_url = res["url"]
                    break
        canonical = c.get("canonicalUrl") or {}
        click = c.get("clickThroughUrl") or {}
        provider = c.get("provider") or {}
        link = (canonical.get("url") if isinstance(canonical, dict) else "") or (
            click.get("url") if isinstance(click, dict) else ""
        )
        records.append({
            "title": str(c.get("title") or "").strip(),
            "summary": str(c.get("summary") or c.get("description") or "").strip(),
            "publisher": provider.get("displayName", "") if isinstance(provider, dict) else "",
            "pub_date": c.get("pubDate") or c.get("displayTime") or "",
            "link": link,
            "thumbnail": thumb_url,
            "editors_pick": bool((c.get("metadata") or {}).get("editorsPick")) if isinstance(c.get("metadata"), dict) else False,
        })
    return ok({"symbol": sym, "rows": records}, meta("yfinance.Ticker.news", len(records)))


@skill(
    "get_us_stock_calendar",
    "美股财报日历（yfinance.Ticker.calendar）：下一次财报日期、预期/前次 EPS、预期/前次 Revenue、"
    "分红日期、Ex-Dividend 日期。symbol 为字母代码。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "美股代码，如 NVDA"},
        },
        "required": ["symbol"],
    },
    internal=True,)
def get_us_stock_calendar(symbol: str) -> dict:
    """美股财报日历（yfinance）。"""
    sym = norm_us_symbol(symbol)
    try:
        cal = yf.Ticker(sym).calendar
    except Exception as e:  # noqa: BLE001
        return err(f"美股 {sym} 财报日历获取失败: {type(e).__name__}: {e}")
    if not cal:
        return err(f"美股 {sym} 暂无财报日历")

    def _to_list(v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, (list, tuple)):
            return [str(x)[:10] for x in v]
        return [str(v)[:10]]

    earnings_dates = _to_list(cal.get("Earnings Date"))
    record = {
        "earnings_dates": earnings_dates,
        "earnings_high": cal.get("Earnings High"),
        "earnings_low": cal.get("Earnings Low"),
        "earnings_average": cal.get("Earnings Average"),
        "revenue_high": cal.get("Revenue High"),
        "revenue_low": cal.get("Revenue Low"),
        "revenue_average": cal.get("Revenue Average"),
        "dividend_date": str(cal.get("Dividend Date") or "")[:10] or None,
        "ex_dividend_date": str(cal.get("Ex-Dividend Date") or "")[:10] or None,
    }
    return ok({"symbol": sym, **record}, meta("yfinance.Ticker.calendar", 1))


@skill(
    "get_us_stock_sec_filings",
    "美股 SEC 文件（yfinance.Ticker.sec_filings 8-K/10-Q/10-K 等）："
    "提交日期/类型/标题/SEC 原始链接/附件。count 控制条数（默认 20，上限 100）。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "美股代码，如 NVDA"},
            "count": {"type": "integer", "description": "返回条数，默认 20，上限 100"},
        },
        "required": ["symbol"],
    },
    internal=True,)
def get_us_stock_sec_filings(symbol: str, count: int = 20) -> dict:
    """美股 SEC 文件（yfinance）。"""
    sym = norm_us_symbol(symbol)
    n = max(1, min(int(count or 20), 100))
    try:
        raw = yf.Ticker(sym).sec_filings or []
    except Exception as e:  # noqa: BLE001
        return err(f"美股 {sym} SEC 文件获取失败: {type(e).__name__}: {e}")
    if not raw:
        return err(f"美股 {sym} 暂无 SEC 文件")
    records = []
    for f in raw[:n]:
        if not isinstance(f, dict):
            continue
        exhibits = f.get("exhibits") or {}
        exhibit_links = list(exhibits.values())[:2] if isinstance(exhibits, dict) else []
        records.append({
            "date": str(f.get("date") or "")[:10],
            "type": f.get("type", ""),
            "title": f.get("title", ""),
            "edgar_url": f.get("edgarUrl", ""),
            "exhibits": exhibit_links,
        })
    return ok({"symbol": sym, "rows": records}, meta("yfinance.Ticker.sec_filings", len(records)))


@skill(
    "get_us_stock_holder",
    "美股股东结构与内部人（yfinance）："
    "major_holders（内部人/机构占比饼图）、institutional_holders（前 10 大机构）、"
    "insider_transactions（近 3 月内部人买入/卖出）、mutualfund_holders（前 10 大基金）。"
    "symbol 为字母代码如 NVDA。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "美股代码，如 NVDA"},
        },
        "required": ["symbol"],
    },
    internal=True,)
def get_us_stock_holder(symbol: str) -> dict:
    """美股股东结构与内部人（yfinance）。"""
    sym = norm_us_symbol(symbol)
    try:
        t = yf.Ticker(sym)
    except Exception as e:  # noqa: BLE001
        return err(f"美股 {sym} 股东数据获取失败: {type(e).__name__}: {e}")
    out: dict[str, Any] = {"symbol": sym}

    def _df_to_records(df, date_cols=("Date",)) -> list[dict]:
        if df is None or len(df) == 0:
            return []
        records = []
        for _, r in df.iterrows():
            d = {}
            for c in df.columns:
                v = r[c]
                if pd.isna(v):
                    continue
                d[str(c)] = str(v) if isinstance(v, (pd.Timestamp,)) else v
            if "Date" in d and hasattr(d["Date"], "isoformat"):
                d["Date"] = d["Date"].isoformat()[:10]
            records.append(d)
        return records

    # 1) major_holders: {0-2 列名可能为 "Breakdown" / "Value"}
    try:
        mh = t.major_holders
        if mh is not None and len(mh) > 0:
            out["major_holders"] = _df_to_records(mh.reset_index())
    except Exception as e:  # noqa: BLE001
        out["major_holders_error"] = f"{type(e).__name__}: {e}"

    # 2) institutional_holders: 含 Date / Holder / Shares / Value / pctChange
    try:
        ih = t.institutional_holders
        if ih is not None and len(ih) > 0:
            out["institutional_holders"] = _df_to_records(ih.head(10))
    except Exception as e:  # noqa: BLE001
        out["institutional_holders_error"] = f"{type(e).__name__}: {e}"

    # 3) mutualfund_holders
    try:
        mfh = t.mutualfund_holders
        if mfh is not None and len(mfh) > 0:
            out["mutualfund_holders"] = _df_to_records(mfh.head(10))
    except Exception as e:  # noqa: BLE001
        out["mutualfund_holders_error"] = f"{type(e).__name__}: {e}"

    # 4) insider_transactions: insider / position / transaction / shares / value / date
    try:
        it = t.insider_transactions
        if it is not None and len(it) > 0:
            out["insider_transactions"] = _df_to_records(it.head(20))
    except Exception as e:  # noqa: BLE001
        out["insider_transactions_error"] = f"{type(e).__name__}: {e}"

    if not any(k in out for k in ("major_holders", "institutional_holders", "mutualfund_holders", "insider_transactions")):
        return err(f"美股 {sym} 股东数据为空")
    return ok(out, meta("yfinance.Ticker.major_holders+institutional+insider", 4))


@skill(
    "get_us_stock_analyst",
    "美股卖方研报评级与目标价（yfinance）："
    "recommendations_summary（近 12 个月评级分布 buy/hold/sell + 0/1 标度）、"
    "analyst_price_targets（最高/最低/平均/中位目标价）、"
    "earnings_estimate（下一季度/年度 EPS/Revenue 共识与范围）、"
    "earnings_history（近 4 季度 EPS estimate/actual/surprise_pct）。"
    "symbol 为字母代码。",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "美股代码，如 NVDA"},
        },
        "required": ["symbol"],
    },
    internal=True,)
def get_us_stock_analyst(symbol: str) -> dict:
    """美股卖方研报评级与目标价（yfinance）。"""
    sym = norm_us_symbol(symbol)
    try:
        t = yf.Ticker(sym)
    except Exception as e:  # noqa: BLE001
        return err(f"美股 {sym} 卖方数据获取失败: {type(e).__name__}: {e}")
    out: dict[str, Any] = {"symbol": sym}

    def _df_to_records(df, n: int | None = None) -> list[dict]:
        if df is None or len(df) == 0:
            return []
        sub = df if n is None else df.head(n)
        records = []
        for _, r in sub.iterrows():
            d = {}
            for c in sub.columns:
                v = r[c]
                if pd.isna(v):
                    continue
                d[str(c)] = str(v) if isinstance(v, (pd.Timestamp,)) else v
            records.append(d)
        return records

    # 1) recommendations_summary: 列含 period / strongBuy / buy / hold / sell / strongSell
    try:
        rs = t.recommendations_summary
        if rs is not None and len(rs) > 0:
            out["recommendations_summary"] = _df_to_records(rs.head(8))
    except Exception as e:  # noqa: BLE001
        out["recommendations_summary_error"] = f"{type(e).__name__}: {e}"

    # 2) analyst_price_targets: 字典（current/low/high/mean/median）
    try:
        apt = t.analyst_price_targets
        if isinstance(apt, dict) and apt:
            out["price_targets"] = {k: (None if (v is None or (isinstance(v, float) and pd.isna(v))) else v)
                                    for k, v in apt.items()}
        elif apt is not None and len(apt) > 0:
            out["price_targets"] = _df_to_records(apt)
    except Exception as e:  # noqa: BLE001
        out["price_targets_error"] = f"{type(e).__name__}: {e}"

    # 3) earnings_estimate: 下一期 EPS/Revenue 共识
    try:
        ee = t.earnings_estimate
        if ee is not None and len(ee) > 0:
            out["earnings_estimate"] = _df_to_records(ee)
    except Exception as e:  # noqa: BLE001
        out["earnings_estimate_error"] = f"{type(e).__name__}: {e}"

    # 4) earnings_history: 近 4 季度 EPS 实际 vs 预期
    try:
        eh = t.earnings_history
        if eh is not None and len(eh) > 0:
            out["earnings_history"] = _df_to_records(eh.head(8))
    except Exception as e:  # noqa: BLE001
        out["earnings_history_error"] = f"{type(e).__name__}: {e}"

    if not any(k in out for k in ("recommendations_summary", "price_targets", "earnings_estimate", "earnings_history")):
        return err(f"美股 {sym} 卖方数据为空")
    return ok(out, meta("yfinance.Ticker.recommendations+price_targets+earnings_estimate", 4))


@skill(
    "get_current_date",
    "返回服务器当前日期与时间（用于对齐「最近/今天」等相对时间表述）。",
    {"type": "object", "properties": {}, "required": []},
    internal=False,)
def get_current_date() -> dict:
    now = datetime.now().astimezone()
    weekdays = "一二三四五六日"
    return ok(
        {
            "date": now.date().isoformat(),
            "datetime": now.isoformat(timespec="seconds"),
            "weekday": f"星期{weekdays[now.weekday()]}",
        },
        meta("server.clock", 1),
    )
