"""Broad event taxonomy and decision requirements for the online catalog.

This layer deliberately separates *catalog inclusion* from *prediction
eligibility*.  Every source record receives a category, while procedural or
underspecified records remain evidence-only until their hard gates pass.
"""
from __future__ import annotations

import re
from typing import Any


PROFILES: dict[str, dict[str, Any]] = {
    "财务业绩与指引": {
        "required_fields": ["报告期", "营收/利润实际值", "同比或环比", "市场预期或公司原指引"],
        "decision_checks": ["实际值相对预期的 surprise", "盈利质量与一次性项目", "指引是否同步变化"],
        "preferred_horizons": ["T1", "T3"],
    },
    "并购重组与资产交易": {
        "required_fields": ["交易阶段", "买卖方身份", "标的资产", "对价与支付方式", "稀释比例或溢价"],
        "decision_checks": ["是否首次披露", "交易对上市公司的直接性", "协同、杠杆与摊薄", "审批和终止条件"],
        "preferred_horizons": ["T1", "T3", "T7"],
    },
    "融资与债务": {
        "required_fields": ["融资工具", "规模", "期限/利率/发行价", "募集用途", "审批或发行阶段"],
        "decision_checks": ["股权稀释或杠杆变化", "流动性改善", "融资成本", "是否仅为程序性进展"],
        "preferred_horizons": ["T1", "T3"],
    },
    "股东持股与控制权": {
        "required_fields": ["增减持主体", "数量与占比", "成交或转让价格", "变动后持股", "控制权是否变化"],
        "decision_checks": ["内部人信号", "供给冲击", "接盘方质量", "是否已执行而非仅计划"],
        "preferred_horizons": ["T1", "T3"],
    },
    "回购分红与股本": {
        "required_fields": ["回购/分红/解禁规模", "占总股本比例", "价格或收益率", "实施日期", "注销或用途"],
        "decision_checks": ["实际资本回报强度", "潜在卖压", "是否已被市场预期", "股本变化"],
        "preferred_horizons": ["T1", "T3"],
    },
    "经营订单与项目": {
        "required_fields": ["合同或项目金额", "相对营收比例", "交易对手", "履约周期", "毛利或收益影响"],
        "decision_checks": ["是否新增订单", "收入确认时点", "可撤销条件", "交易对手信用与执行风险"],
        "preferred_horizons": ["T1", "T3"],
    },
    "产品研发与许可": {
        "required_fields": ["产品或适应症", "研发/审批阶段", "关键结果", "商业化时间", "市场空间或收入贡献"],
        "decision_checks": ["结果是否达到主要终点", "监管意义", "后续投入与失败概率", "收入兑现距离"],
        "preferred_horizons": ["T1", "T3"],
    },
    "监管法律与重大风险": {
        "required_fields": ["监管/诉讼主体", "事项阶段", "涉案或处罚金额", "最坏损失", "经营与上市状态影响"],
        "decision_checks": ["新增风险还是旧案进展", "损失相对净资产", "停产/退市概率", "是否已有计提"],
        "preferred_horizons": ["T1", "T3"],
    },
    "治理人事与激励": {
        "required_fields": ["人员或激励对象", "职务/数量", "变更原因", "授予价格", "业绩考核条件"],
        "decision_checks": ["关键人依赖", "治理稳定性", "激励稀释", "考核目标强弱"],
        "preferred_horizons": ["T1", "T3"],
    },
    "交易状态与市场异动": {
        "required_fields": ["停复牌或异动原因", "生效时间", "是否存在未披露重大事项", "复牌条件"],
        "decision_checks": ["可交易性", "信息是否仅为澄清", "事前涨跌与拥挤度", "恢复交易时间"],
        "preferred_horizons": ["T1", "T3"],
    },
    "宏观政策与行业冲击": {
        "required_fields": ["公布值", "一致预期", "前值", "政策变化幅度", "受影响行业或资产"],
        "decision_checks": ["surprise 方向与幅度", "传导链", "政策预期是否提前定价", "基准选择"],
        "preferred_horizons": ["T1", "T3"],
    },
    "信息沟通与程序文件": {
        "required_fields": ["所依附的主事件", "是否包含新增实质条款", "原始主文件链接"],
        "decision_checks": ["默认不独立预测", "仅在出现新增金额、结论或终止条件时升级"],
        "preferred_horizons": [],
    },
    "其他待分类": {
        "required_fields": ["事件主体", "新增事实", "金额或影响范围", "生效时间"],
        "decision_checks": ["先人工或模型分类", "不能仅凭标题给方向"],
        "preferred_horizons": [],
    },
}


# These are calibration anchors, not hard-coded up/down labels.  They tell the
# model which denominator, baseline, or event stage should be used before it
# judges materiality.
MATERIALITY_ANCHORS: dict[str, list[str]] = {
    "财务业绩与指引": ["优先使用实际值相对一致预期", "无一致预期时以原指引和历史季节性为次级基准", "一次性损益必须拆出"],
    "并购重组与资产交易": ["交易对价/市值或总资产", "意向、预案、批准、交割、终止分阶段", "反复进展不重复计为新事件"],
    "融资与债务": ["潜在稀释比例和发行折溢价", "融资额/市值或净资产", "债务成本、期限和偿付压力"],
    "股东持股与控制权": ["变动股数/流通股本", "计划披露与实际成交分开", "控制权变化高于普通持股变动"],
    "回购分红与股本": ["回购或分红金额/市值", "解禁股数/流通股本", "新方案、实施和月度进展分开"],
    "经营订单与项目": ["合同额/最近十二个月营收", "预计利润贡献而非只看总额", "框架协议低于确定订单"],
    "产品研发与许可": ["受理、临床批准、关键终点、上市批准严格分级", "产品收入潜力/公司营收", "普通专利和证照续期默认低实质性"],
    "监管法律与重大风险": ["最大风险敞口/净资产或年度利润", "立案、判决、执行和结案分阶段", "经营许可、停产和上市资格提高权重"],
    "治理人事与激励": ["董事长、CEO、CFO和实控人高于普通董监高", "激励摊薄比例", "考核目标相对历史和一致预期"],
    "交易状态与市场异动": ["停复牌改变可交易性但不天然决定方向", "异常波动公告本身默认无新增方向", "风险警示的新增原因优先于标签名称"],
    "宏观政策与行业冲击": ["实际值减一致预期并按历史波动标准化", "政策变化相对会前定价", "公司或行业暴露度决定传导强度"],
    "信息沟通与程序文件": ["默认只挂接主事件", "只有新增金额、关键结论、审批或终止条件时升级并重新分类"],
    "其他待分类": ["不得直接预测", "先归类并建立适当分母后再评分"],
}


_RULES: list[tuple[str, str]] = [
    ("财务业绩与指引", r"业绩预告|业绩快报|年度报告|半年度报告|季度报告|定期报告|盈利预测|业绩指引|经营目标|10-[QK]|20-F|40-F"),
    ("并购重组与资产交易", r"重大资产重组|发行股份购买资产|要约收购|收购报告|吸收合并|分拆|资产置换|收购出售资产|股权转让|S-4|DEFM14A|PREM14A|Rule 425"),
    ("回购分红与股本", r"股份回购|回购进展|回购报告|利润分配|分配方案|权益分派|限售股份|解禁|股本变动"),
    ("股东持股与控制权", r"股份减持|股份增持|权益变动|实际控制人|控制权|协议转让|股份质押|股份冻结|持股变动|SCHEDULE 13|Form 4|Form 3|144"),
    ("融资与债务", r"增发|配股|可转债|公司债|债券|募集资金|借贷|担保|再融资|首发|S-8|424B|FWP"),
    ("经营订单与项目", r"重大合同|中标|签订协议|经营情况|月度经营|销售情况|对外项目投资|投资设立公司|获得补贴|政府补助"),
    ("产品研发与许可", r"临床试验|受试者|适应症|产品获批|注册证|专利|认证|研发|许可"),
    ("监管法律与重大风险", r"诉讼|仲裁|处罚|立案|问询函|风险提示|退市|终止上市|自查报告|监管"),
    ("治理人事与激励", r"高管人员|董事|监事|股权激励|员工持股|审计机构变更|公司章程|股东大会|董事会决议"),
    ("交易状态与市场异动", r"停牌|复牌|异常波动|风险警示|ST"),
    ("宏观政策与行业冲击", r"CPI|PPI|PCE|通胀|非农|失业率|PMI|GDP|利率|FOMC|LPR|MLF|政策"),
    ("信息沟通与程序文件", r"调研活动|投资者关系|说明会|法律意见|保荐|核查意见|专项说明|独立意见|会议资料|管理办法|议事规则|提示性公告|进展公告|回复问询"),
]

# Native exchange categories are more reliable than words embedded in a long
# title (for example, “研发生产系统员工持股计划” is governance, not R&D).
_NATIVE_RULES: list[tuple[str, str]] = [
    ("信息沟通与程序文件", r"调研活动|法律意见|保荐|核查意见|专项说明|独立意见|股东大会资料|会议资料|管理办法|制度"),
    ("交易状态与市场异动", r"停复牌|股票交易异常波动|风险警示"),
    ("监管法律与重大风险", r"诉讼|仲裁|处罚|立案|问询函|终止上市|退市|风险提示"),
    ("财务业绩与指引", r"业绩预告|业绩快报|年度报告|半年度报告|季度报告|半年报|10-Q|10-K|20-F|40-F"),
    ("并购重组与资产交易", r"重大资产重组|收购报告|要约收购|吸收合并|分拆|资产置换|收购出售资产|股权转让"),
    ("回购分红与股本", r"回购|分配方案|权益分派|限售股份|解禁|股本变动"),
    ("股东持股与控制权", r"减持|增持|权益变动|控制权|实际控制人|质押|冻结|高管人员持股变动"),
    ("融资与债务", r"增发|配股|可转债|公司债|债券|募集资金|借贷|担保|再融资|首发|424B|FWP|S-8"),
    ("经营订单与项目", r"重大合同|中标|签订协议|经营情况|销售情况|投资设立公司|政府补助"),
    ("产品研发与许可", r"临床试验|适应症|产品获批|注册证|专利|获得认证"),
    ("治理人事与激励", r"高管人员任职变动|董事|监事|股权激励|员工持股|审计机构变更|公司章程|股东大会|董事会决议"),
]

_PROCEDURAL = re.compile(
    r"调研活动|投资者关系|说明会|法律意见|保荐|核查意见|专项说明|独立意见|"
    r"会议资料|管理办法|议事规则|提示性公告|持续督导|董事会决议|股东大会|公司章程",
    re.I,
)
_HIGH_NOVELTY = re.compile(
    r"首次|预案|草案|报告书|终止|失败|完成交割|过户完成|处罚决定|立案|重大合同|中标|"
    r"业绩预告|业绩快报|10-Q|10-K|20-F|8-K",
    re.I,
)
_ROUTINE_EVIDENCE = re.compile(
    r"回购进展|权益分派实施|分配方案实施|限售股份上市流通|募集资金使用进展|"
    r"募集资金使用情况报告|闲置募集资金|现金管理|投资理财|专户存储|三方监管协议|"
    r"英文版|提示性公告|申请获受理",
    re.I,
)
_REVIEW_NEEDED = re.compile(
    r"提供.*担保|对外担保|股份质押|股份冻结|获得认证|专利证书|发明专利|"
    r"注册证书|注册证|更正公告|修订版|股票交易异常波动",
    re.I,
)


def classify_event(native_type: str, title: str, market: str) -> dict[str, Any]:
    pool = f"{native_type or ''} {title or ''}"
    category = "其他待分类"
    matched = "fallback"
    native = native_type or ""
    normalized_native = native.strip().upper()
    if str(market or "").upper() == "US" and normalized_native in {"3", "4", "144"}:
        category = "股东持股与控制权"
        matched = "native:US ownership form"
    # “其他” and similarly generic native labels carry no semantic priority.
    if category == "其他待分类" and native.strip() not in {"", "其他"}:
        for candidate, pattern in _NATIVE_RULES:
            if re.search(pattern, native, re.I):
                category = candidate
                matched = f"native:{pattern}"
                break
    if category == "其他待分类":
        for candidate, pattern in _RULES:
            if re.search(pattern, pool, re.I):
                category = candidate
                matched = f"title:{pattern}"
                break

    # These phrases refer to corporate accounting/communication or transaction
    # review policy; they are not macro releases merely because they contain 政策.
    if re.search(r"会计政策|股东通讯政策|产业政策和交易类型", pool, re.I):
        category = "信息沟通与程序文件"
        matched = "corporate_policy_not_macro"

    # A generic exchange label can be less informative than a specific title.
    if re.search(r"重大资产重组|发行股份.*购买资产|要约收购|收购报告书", title, re.I):
        category = "并购重组与资产交易"
        matched = "specific_mna_title_override"
    elif re.search(r"财务资助", title, re.I):
        category = "融资与债务"
        matched = "financial_assistance_title_override"

    if category == "其他待分类":
        for candidate, pattern in _RULES:
            if re.search(pattern, pool, re.I):
                category = candidate
                matched = f"fallback:{pattern}"
                break

    # Routine registration/offering supplements and ownership forms are useful
    # evidence, but usually are not a new standalone price event by themselves.
    routine_us_form = normalized_native in {
        "424B2", "424B3", "424B5", "FWP", "S-8", "3", "4", "144",
        "FORM 3", "FORM 4", "FORM 144"
    }

    if (category == "信息沟通与程序文件" or _PROCEDURAL.search(pool)
            or routine_us_form or _ROUTINE_EVIDENCE.search(pool)):
        role = "evidence_only"
        information_tier = "low"
    elif category == "其他待分类":
        role = "review"
        information_tier = "unknown"
    elif category in {"治理人事与激励", "交易状态与市场异动"} or _REVIEW_NEEDED.search(pool):
        role = "review"
        information_tier = "medium"
    else:
        role = "prediction_candidate"
        information_tier = "high" if _HIGH_NOVELTY.search(pool) else "medium"

    profile = PROFILES[category]
    return {
        "normalized_event_type": category,
        "catalog_role": role,
        "information_tier": information_tier,
        "taxonomy_match": matched,
        "required_fields": profile["required_fields"],
        "decision_checks": profile["decision_checks"],
        "preferred_horizons": profile["preferred_horizons"],
        "market": market,
    }
