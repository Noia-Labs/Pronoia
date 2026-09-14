"""一次性生成 5 个小规模真实感数据集（各 10 events），并注册到 bt_datasets。

用法：
  /Users/vix/Code/PRONOIA/.venv/bin/python scripts/generate_datasets.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

# 让 app 包可 import
BACKEND_DIR = "/Users/vix/Code/PRONOIA/backend"
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app import db  # noqa: E402
from app.event_backtest.application import load_events  # noqa: E402
from app.event_backtest.engine import validate_events  # noqa: E402

ROOT = Path("/Users/vix/Code/PRONOIA")
DS_DIR = ROOT / "data" / "datasets"
DS_DIR.mkdir(parents=True, exist_ok=True)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} rows -> {path}")


# ===================================================================== 数据集 1
def ds_cn_earnings_q2() -> tuple[str, str, list[dict], list[dict]]:
    ds_id = "cn_earnings_q2"
    name = "A股半年报 · 2026Q2（核心资产 10 例）"
    stocks = [
        ("600519", "贵州茅台",   "up",    "营收与净利润双位数增长，毛利率稳中有升，直销渠道占比继续提升"),
        ("000858", "五粮液",     "up",    "上半年营收同比增长12%，分红比例提升至70%"),
        ("300750", "宁德时代",   "up",    "全球动力电池市占率首超40%，海外收入占比首次过半"),
        ("002594", "比亚迪",     "up",    "新能源汽车销量同比+38%，出口量翻倍，单车盈利环比改善"),
        ("600036", "招商银行",   "down",  "息差进一步收窄至2.01%，财富管理中收同比下滑8%"),
        ("601318", "中国平安",   "down",  "新业务价值NBV同比下降3%，寿险代理人规模继续缩量"),
        ("000001", "平安银行",   "down",  "不良率环比上行12bp，房地产业务敞口拨备计提增加"),
        ("002415", "海康威视",   "up",    "创新业务（机器人/汽车电子）同比+25%，海外业务止跌回升"),
        ("600887", "伊利股份",   "neutral","常温液奶承压，奶粉小幅改善，整体符合一致预期"),
        ("600690", "海尔智家",   "up",    "海外品牌卡萨帝/GEA利润率创新高，现金流转正"),
    ]
    events, labels = [], []
    for i, (symbol, name_cn, direction, summary) in enumerate(stocks, start=1):
        eid = f"{ds_id}-{i:03d}"
        title = f"{name_cn}（{symbol}）披露2026年半年报"
        # 所有事件日期必须落在"今天（2026-08-16）之前"，避免 strict-as-of 报错，也避免 UI 上出现"未来公告"。
        event_time = f"2026-06-{10 + i:02d}T07:30:00+08:00"
        source_url = f"https://www.cninfo.com.cn/new/disclosure/detail?stockCode={symbol}&announcementId=ann2026h1_{eid}"
        event_text = (
            f"{name_cn}（证券代码：{symbol}）于{event_time[:10]}晚间披露2026年半年度报告。\n"
            f"核心看点：{summary}。\n"
            f"公司同时公告拟于9月召开临时股东大会审议利润分配预案。分析师指出，当前时点需关注后续需求修复节奏与竞争格局变化，"
            f"半年报业绩落地后，短期资金博弈可能加大波动。\n"
            f"本次公告由公司董事会审议通过，全体董事签字确认，详情见巨潮资讯网原文链接。"
        )
        events.append({
            "event_id": eid, "market": "CN", "symbol": symbol,
            "event_time": event_time, "event_type_l2": "earnings_report",
            "title": title, "event_text": event_text, "source_url": source_url,
            "benchmark": "sh000300", "direction_prior": None, "event_strength": 6 if direction != "neutral" else 4,
        })
        labels.append({"event_id": eid, "label": direction, "source": "oracle_q2_2026",
                       "t3_return_pct": +4.2 if direction == "up" else -3.8 if direction == "down" else +0.4})
    return ds_id, name, events, labels


# ===================================================================== 数据集 2
def ds_cn_insiders() -> tuple[str, str, list[dict], list[dict]]:
    ds_id = "cn_insiders"
    name = "A股增减持·回购（大宗与股东行为 10 例）"
    items = [
        ("300059", "东方财富", "控股股东一致行动人计划6个月内增持不低于5亿元", "up"),
        ("601888", "中国中免", "二季度获北向资金连续15日净买入，累计+38亿元", "up"),
        ("603259", "药明康德", "实际控制人拟通过大宗交易减持不超过总股本3%，用于偿还个人债务", "down"),
        ("002352", "顺丰控股", "董事会批准20亿元回购方案，价额上限65元/股，期限12个月", "up"),
        ("601899", "紫金矿业", "海外重要股东 Barrick Gold 减持约1.2亿股 H 股，套现价约18港元/股", "down"),
        ("002475", "立讯精密", "董事长及核心高管以集合资产管理计划增持1200万股，成交均价约32元", "up"),
        ("600900", "长江电力", "长江三峡集团完成本年度增持计划，累计增持0.8%总股本", "neutral"),
        ("600031", "三一重工", "回购股份注销进展：累计回购注销2.3%总股本，减少流通供给", "up"),
        ("000568", "泸州老窖", "多名董事/监事披露减持计划窗口期一过即全部放弃减持，维持稳定承诺", "neutral"),
        ("600030", "中信证券", "中信集团按持股比例参与配股，斥资120亿元足额认购，巩固控制权", "up"),
    ]
    events, labels = [], []
    for i, (symbol, name_cn, action, direction) in enumerate(items, start=1):
        eid = f"{ds_id}-{i:03d}"
        title = f"{name_cn}（{symbol}）：{action[:22]}…"
        event_time = f"2026-07-{(i * 3) % 28 + 1:02d}T20:05:00+08:00"
        source_url = f"https://www.cninfo.com.cn/new/disclosure/detail?stockCode={symbol}&announcementId=insider_{eid}"
        event_text = (
            f"{name_cn}（{symbol}）公告如下股东/董事/监事/高管变动或回购事项：\n"
            f"{action}。\n"
            f"本次变动属于上交所/深交所《上市公司股东及董事、监事、高级管理人员减持股份实施细则》规范范畴，"
            f"涉及主体已依法履行信息披露义务，相关交易需在后续定期报告中作进一步说明。"
        )
        events.append({
            "event_id": eid, "market": "CN", "symbol": symbol,
            "event_time": event_time, "event_type_l2": "insider_trading",
            "title": title, "event_text": event_text, "source_url": source_url,
            "benchmark": "sh000300", "direction_prior": None, "event_strength": 5,
        })
        labels.append({"event_id": eid, "label": direction, "source": "oracle_insiders",
                       "t3_return_pct": +3.1 if direction == "up" else -2.9 if direction == "down" else +0.2})
    return ds_id, name, events, labels


# ===================================================================== 数据集 3
def ds_cn_policy() -> tuple[str, str, list[dict], list[dict]]:
    ds_id = "cn_policy"
    name = "宏观·行业政策（国常会/央行/监管 10 例）"
    items = [
        ("600036", "国常会：研究出台进一步促进居民消费的一揽子政策措施",
         "会议要求金融机构降低消费信贷成本，鼓励汽车、家电以旧换新，银行消费贷利率上限引导性下调30-50bp。招商银行有望在零售端受益于消费回暖。", "up", "macro_policy"),
        ("601318", "银保监会：人身险产品定价利率评估机制优化，传统险预定利率上限由3%下调至2.5%",
         "新规自2026年9月1日起执行。存量产品不受影响；新单内含价值将承压，但行业利差损风险下降。短期中国平安新单增速面临波动。", "down", "industry_policy"),
        ("000002", "住建部+央行联合发文：再推出3000亿专项借款用于支持已售逾期难交付住宅项目",
         "首批资金8月底前拨付，重点覆盖长三角、珠三角、川渝地区项目。万科A未出现在首批出险名单，但其供应链受益。", "up", "industry_policy"),
        ("600028", "发改委：非居民用气门站价格并轨，推行市场化定价，基准价上浮空间扩至20%",
         "中国石化作为上游气源供应商，在冬季保供旺季气价弹性改善，季度现金流预期上调约4%。", "up", "macro_policy"),
        ("601857", "财政部：石油特别收益金起征点由65美元/桶上调至75美元/桶",
         "按国际油价80美元/桶估算，中国石油集团全年减负约120亿元人民币，直接增厚归母净利润。", "up", "macro_policy"),
        ("600900", "国家能源局：出台《抽水蓄能电站电价机制指引》，容量电价单列保障6.5%资本金IRR",
         "长江电力在建抽蓄项目达12GW，指引落地后投资回收确定性显著提升，估值底部抬升。", "up", "industry_policy"),
        ("300750", "欧盟委员会：对产自中国的动力电池发起反补贴调查，初裁结果预计Q4公布",
         "涉案企业包含宁德时代欧洲工厂。若被加征关税，公司欧洲本地化产能扩张节奏可能放慢。", "down", "industry_policy"),
        ("688981", "国务院关税税则委员会：对部分集成电路关键设备零部件实施为期3年的进口暂定零关税",
         "中芯国际扩产产线采购成本预计下降3-5个百分点，年度capex节约约人民币18亿元。", "up", "industry_policy"),
        ("000725", "工信部：启动新一轮电子信息制造业提质升级行动，面板行业新增产能窗口指导收紧",
         "供给端无序扩张得到约束，京东方A行业定价权恢复，LCD周期底部或提前确认。", "up", "industry_policy"),
        ("000300", "央行：降准0.5个百分点释放长期流动性约1万亿元，MLF利率下调10bp",
         "宽货币组合释放稳增长信号，债市收益率曲线牛陡，权益市场估值修复预期加强，沪深300指数短期风险偏好改善。", "up", "macro_policy"),
    ]
    events, labels = [], []
    for i, (symbol, title_piece, summary, direction, l2) in enumerate(items, start=1):
        eid = f"{ds_id}-{i:03d}"
        title = title_piece
        day = (i * 2) % 28 + 1
        # 全部落在 2026-05 / 06（今天 2026-08-16 之前），避免出现"未来政策"
        month = 5 + (i % 2)
        event_time = f"2026-{month:02d}-{day:02d}T19:00:00+08:00"
        source_url = f"https://www.gov.cn/zhengce/2026-{month:02d}-{day:02d}/content_{eid}.htm"
        event_text = (
            f"{title_piece}\n\n"
            f"{summary}\n\n"
            f"政策文件自公布之日起施行，配套细则由相关主管部门负责解释。机构点评认为，该政策落地后将对相关行业中期供需结构产生方向性影响。"
        )
        events.append({
            "event_id": eid, "market": "CN", "symbol": symbol,
            "event_time": event_time, "event_type_l2": l2,
            "title": title, "event_text": event_text, "source_url": source_url,
            "benchmark": "sh000300", "direction_prior": None, "event_strength": 7,
        })
        labels.append({"event_id": eid, "label": direction, "source": "oracle_policy",
                       "t3_return_pct": +2.7 if direction == "up" else -3.5 if direction == "down" else 0.0})
    return ds_id, name, events, labels


# ===================================================================== 数据集 4
def ds_us_earnings_q2() -> tuple[str, str, list[dict], list[dict]]:
    ds_id = "us_earnings_q2"
    name = "美股 2026Q2 财报（Mag7 + 半导体/金融 10 例）"
    items = [
        ("NVDA", "NVIDIA Corporation",   "up",    "FY2027Q2 revenue +182% YoY, data center GPUs backlog extends to 2027Q3; guided next quarter above street. Gross margin expands to 77%.", "earnings_report"),
        ("AAPL", "Apple Inc.",           "up",    "Services revenue reached all-time high, iPhone ASP up mid-single digits; buyback upsized by +$110bn, dividend +5%.", "earnings_report"),
        ("MSFT", "Microsoft Corp.",      "up",    "Azure AI services annualized run-rate crosses $30bn, Copilot seat penetration >60% within Fortune 500; EBIT margin 48%.", "earnings_report"),
        ("GOOGL","Alphabet Inc.",        "neutral","Search ads growth +7% YoY in line; Cloud margin disappoints; Waymo losses widen. No material beat vs. consensus.", "earnings_report"),
        ("AMZN", "Amazon.com Inc.",      "up",    "AWS operating margin +380bps, Advertising +22%, Retail logistics unit first half FCF record high. Raised full-year capex guidance.", "earnings_report"),
        ("META", "Meta Platforms Inc.",  "up",    "Reels monetization closes gap with Feed; Reality Labs losses narrow. Operating margin guide raised 3pp to 37-38%.", "earnings_report"),
        ("TSLA", "Tesla Inc.",           "down",  "Q2 deliveries missed -8% q/q, margin compressed on price cuts, Cybertruck recall ongoing; Robotaxi unveil pushed to year-end.", "earnings_report"),
        ("AMD",  "Advanced Micro Devices","down",  "MI400 ramp slower than expected, Enterprise segment -3% q/q; data center bookings pushed into Q4; guided Q3 below street.", "earnings_report"),
        ("NFLX", "Netflix Inc.",         "up",    "Paid net adds +8.4M, ad-tier ARPU up 15% qoq, free cash flow beats; $25bn buyback announced.", "earnings_report"),
        ("JPM",  "JPMorgan Chase & Co.", "up",    "Net Interest Income record on rate tail, IB fees +22% YoY, provisions light; CEO Dimon reiterated capital return plan.", "earnings_report"),
    ]
    events, labels = [], []
    for i, (symbol, name_en, direction, summary, l2) in enumerate(items, start=1):
        eid = f"{ds_id}-{i:03d}"
        title = f"{name_en} ({symbol}) Q2 2026 Earnings"
        day = (i * 2) % 27 + 1
        event_time = f"2026-07-{day:02d}T20:00:00-04:00"
        if symbol == "GOOGL":
            source_url = f"https://abc.xyz/investor/quarterly/2026q2/{eid}"
        elif symbol == "JPM":
            source_url = f"https://www.jpmorganchase.com/ir/quarterly-results/2026-q2-{eid}"
        else:
            source_url = f"https://ir.{symbol.lower()}.com/financials/quarterly/q2-2026/{eid}"
        event_text = (
            f"{name_en} ({symbol}) reported Q2 FY2026 earnings after market close on {event_time[:10]}.\n"
            f"Key takeaways: {summary}\n\n"
            f"Management will host an earnings webcast at 5:00 PM ET. This summary is drafted from the 8-K press release; GAAP reconciliation tables and supplemental slides are available via the IR link above."
        )
        benchmark = "QQQ" if symbol in {"NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AMD", "NFLX"} else "SPY"
        events.append({
            "event_id": eid, "market": "US", "symbol": symbol,
            "event_time": event_time, "event_type_l2": l2,
            "title": title, "event_text": event_text, "source_url": source_url,
            "benchmark": benchmark, "direction_prior": None, "event_strength": 7,
        })
        labels.append({"event_id": eid, "label": direction, "source": "oracle_us_earnings_q2",
                       "t3_return_pct": +5.8 if direction == "up" else -6.4 if direction == "down" else -0.1})
    return ds_id, name, events, labels


# ===================================================================== 数据集 5
def ds_hk_ratings() -> tuple[str, str, list[dict], list[dict]]:
    ds_id = "hk_ratings"
    name = "港股评级调整（国际行覆盖 10 例）"
    items = [
        ("00700", "腾讯控股",       "Goldman Sachs", "Buy",         "520 HKD", "up",      "维持买入评级，目标价由470上调至520，核心逻辑为游戏出海+企业微信广告货币化"),
        ("09988", "阿里巴巴-SW",    "Morgan Stanley","Overweight", "135 HKD", "neutral", "评级未变，仅调整分部加总估值，新目标价反映云业务重组完成"),
        ("03690", "美团-W",         "JPMorgan",     "Overweight", "190 HKD", "up",      "本地生活take rate提升空间被低估，上调评级至增持，目标价190"),
        ("09618", "京东集团-SW",    "UBS",          "Buy",         "175 HKD", "up",      "1P零售业务利润率拐点确认，管理层全年指引上调，升至买入"),
        ("09888", "百度集团-SW",    "Citi",         "Neutral",     "95 HKD",  "neutral", "文心一言商业化节奏慢于同业，AI capex回收周期存疑，维持中性"),
        ("01024", "快手-W",         "BofA",         "Underperform","48 HKD",  "down",    "用户时长环比下滑，广告主投放ROI收缩，降评级至跑输大市"),
        ("01810", "小米集团-W",     "CLSA",         "Outperform",  "32 HKD",  "up",      "SU7 Ultra订单超预期，IoT毛利率同比+2.1pct，上调目标价至32"),
        ("02318", "中国平安",       "HSBC Global",  "Hold",        "52 HKD",  "neutral", "寿险NBV拐点未至，维持持有，待下半年开门红数据再评估"),
        ("00939", "建设银行",       "Deutsche Bank","Buy",         "7.8 HKD", "up",      "中报拨备压力小于同行，股息率7%+提供安全边际，升至买入"),
        ("00883", "中国海洋石油",   "CICC",         "Outperform",  "22 HKD",  "up",      "桶油成本继续下行，派息政策维持，中海油上调至推荐，目标价22"),
    ]
    events, labels = [], []
    for i, (symbol, name_cn, bank, rating, tgt, direction, note) in enumerate(items, start=1):
        eid = f"{ds_id}-{i:03d}"
        title = f"{bank} 调整 {name_cn}（{symbol}）评级至 {rating}，目标价 {tgt}"
        day = (i * 3) % 27 + 1
        # 全部落在 2026-06 / 07（今天 2026-08-16 之前），避免出现"未来评级调整"
        month = 6 + (i % 2)
        event_time = f"2026-{month:02d}-{day:02d}T09:30:00+08:00"
        safe_bank = bank.lower().replace(" ", "-").replace(".", "")
        source_url = f"https://research.{safe_bank}.hk/reports/{eid}"
        event_text = (
            f"{bank} 研究部于 2026-{month:02d}-{day:02d} 发布个股报告，调整 {name_cn}（港股代码 {symbol}）投资评级。\n"
            f"新评级：{rating}，新目标价：{tgt}。\n"
            f"主要依据：{note}。\n"
            f"本评级为该行标准化股票评级体系下的正式报告，分析师已完成合规利益冲突披露，全文见研究部官网。"
        )
        events.append({
            "event_id": eid, "market": "HK", "symbol": symbol,
            "event_time": event_time, "event_type_l2": "rating_change",
            "title": title, "event_text": event_text, "source_url": source_url,
            "benchmark": "HSI", "direction_prior": None, "event_strength": 5,
        })
        labels.append({"event_id": eid, "label": direction, "source": "oracle_hk_ratings",
                       "t3_return_pct": +3.9 if direction == "up" else -4.2 if direction == "down" else +0.1})
    return ds_id, name, events, labels


# ===================================================================== Main
DATASETS = [ds_cn_earnings_q2, ds_cn_insiders, ds_cn_policy, ds_us_earnings_q2, ds_hk_ratings]


def upsert_dataset(ds_id: str, name: str, events_path: Path, labels_path: Path,
                   events: list[dict]) -> dict:
    by_market = Counter(e["market"] for e in events)
    by_type   = Counter(e["event_type_l2"] for e in events)
    by_symbol = Counter(e["symbol"] for e in events)
    times = sorted(e["event_time"] for e in events if e.get("event_time"))
    date_range = {"min": times[0], "max": times[-1]} if times else None
    return db.upsert_bt_dataset(
        dataset_id=ds_id,
        path=str(events_path),
        name=name,
        total_events=len(events),
        by_market=dict(by_market),
        by_type=dict(by_type),
        by_symbol=dict(by_symbol),
        date_range=date_range,
        labels_path=str(labels_path),
    )


def main() -> int:
    for maker in DATASETS:
        ds_id, name, events, labels = maker()
        events_path = DS_DIR / ds_id / "events.jsonl"
        labels_path = DS_DIR / ds_id / "labels.jsonl"
        write_jsonl(events_path, events)
        write_jsonl(labels_path, labels)

        evs = load_events(str(events_path))
        issues = validate_events(evs)
        if issues:
            print(f"[FAIL] {ds_id} validation failed:\n  " + "\n  ".join(issues), file=sys.stderr)
            continue
        print(f"[ OK ] {ds_id} 已通过 validate_events，{len(evs)} 条。")

        row = upsert_dataset(ds_id, name, events_path, labels_path, events)
        print(f"[ OK ] upsert -> bt_datasets: {row.get('id')} / {row.get('name')} / total_events={row.get('total_events')}")

    print("\nDatasets registered in DB:")
    for r in db.list_bt_datasets():
        print(f"  - {str(r.get('id','')):<20s} {int(r.get('total_events',0)):>3d}ev  by_market={r.get('by_market')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
