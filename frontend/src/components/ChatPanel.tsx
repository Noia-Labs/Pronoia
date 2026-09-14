import { useEffect, useRef, useState } from "react";
import {
  CandlestickChart,
  Landmark,
  LineChart,
  Newspaper,
  Sparkles,
  TrendingUp,
  Users,
} from "lucide-react";
import { useStore } from "../store";
import { api } from "../api";
import type { SuggestionItem } from "../types";
import Composer from "./Composer";
import MessageItem from "./MessageItem";

type Suggestion = SuggestionItem;

function suggestionPrompt(s: Suggestion): string {
  const prompt = (s.query ?? s.text).trim();
  return prompt || s.text.trim();
}

/* ---------- 静态推荐池：每池 ≥4 条，「换一批」时随机抽 2 条/池，凑成 6 条 ---------- */

const POOL_AUTO: Suggestion[] = [
  {
    text: "看下今天的财经快讯",
    mode: "auto",
    icon_hint: "newspaper",
    desc: "全网快讯筛高影响事件",
    query: "看下今天的重要财经快讯，筛出 3 条对市场影响最大的",
  },
  {
    text: "分析 A 股今日整体行情",
    mode: "auto",
    icon_hint: "candlestick",
    desc: "指数 + 板块 + 龙虎榜",
    query: "分析今天 A 股整体行情：主要指数、领涨/领跌板块、龙虎榜异动",
  },
  {
    text: "看下最新宏观指标",
    mode: "auto",
    icon_hint: "landmark",
    desc: "CPI / PMI / M2 / 国债",
    query: "看下最近发布的宏观指标（CPI / PMI / M2 / 10 年期国债收益率）",
  },
  {
    text: "看最近的行业资金流向",
    mode: "auto",
    icon_hint: "trending",
    desc: "行业资金净流入 + 板块异动",
    query: "看最近的行业资金净流入和板块异动，筛 3 个最值得关注的",
  },
];

const POOL_AGENT: Suggestion[] = [
  {
    text: "用事件猎手扫高影响事件",
    mode: "agent",
    agent: "event_scout",
    icon_hint: "sparkles",
    desc: "事件猎手 · 新闻 + 公告 + 异动",
    query: "扫描最近 24 小时的财经新闻和公告，筛出 5 条对市场影响最大的事件",
  },
  {
    text: "用行情分析师看比亚迪 K 线",
    mode: "agent",
    agent: "market_analyst",
    icon_hint: "candlestick",
    desc: "行情分析师 · K线 + 板块 + 龙虎榜",
    query: "分析 002594 比亚迪近 60 个交易日的 K 线走势、技术指标、量价关系",
  },
  {
    text: "用基本面分析师看宁德时代",
    mode: "agent",
    agent: "fundamentals_analyst",
    icon_hint: "landmark",
    desc: "基本面分析师 · 财务摘要 + 研报",
    query: "用财务摘要 + 研报评级，分析 300750 宁德时代近三年的财务健康度和机构观点",
  },
  {
    text: "用预测员推演茅台后市",
    mode: "agent",
    agent: "predictor",
    icon_hint: "trending",
    desc: "预测员 · 3 档情景 + 概率 + 催化",
    query: "用世界模型推演 600519 贵州茅台后市的 3 种情景（乐观/中性/悲观）以及概率和关键催化",
  },
];

const POOL_TEAM: Suggestion[] = [
  {
    text: "团队研究：分析军工板块异动",
    mode: "team",
    icon_hint: "users",
    desc: "研究团队 · 多专家并行 + 复核",
    query: "今天军工板块异动原因分析：涉及个股、产业链传导、是否可持续（团队模式）",
  },
  {
    text: "团队研究：对英伟达做深度研究",
    mode: "team",
    icon_hint: "users",
    desc: "研究团队 · 证据图谱沉淀",
    query: "对 NVDA 英伟达做深度研究：近期财报、AI 需求、竞争格局、估值（团队模式）",
  },
  {
    text: "团队研究：地产政策对银行股影响",
    mode: "team",
    icon_hint: "users",
    desc: "研究团队 · 多角度交叉验证",
    query: "近期地产政策对银行股的影响路径、受益板块、风险点（团队模式）",
  },
  {
    text: "团队研究：黄金价格后市跟踪",
    mode: "team",
    icon_hint: "users",
    desc: "研究团队 · 宏观 + 资金 + 行情",
    query: "跟踪近期黄金价格走势：美元/利率/地缘因素如何影响？后市如何看？（团队模式）",
  },
];

/** Fisher-Yates 洗牌，返回新数组 */
function shuffle<T>(arr: T[]): T[] {
  const a = [...arr];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

/** 从池中随机抽 2 条 */
function pickTwo<T>(arr: T[]): T[] {
  return shuffle(arr).slice(0, 2);
}

/** 6 条推荐：2 快速 + 2 专家 + 2 团队（顺序固定，分组清晰） */
function pickSix(): Suggestion[] {
  return [...pickTwo(POOL_AUTO), ...pickTwo(POOL_AGENT), ...pickTwo(POOL_TEAM)];
}

const ICON_BY_HINT: Record<string, React.ReactNode> = {
  newspaper: <Newspaper size={16} />,
  sparkles: <Sparkles size={16} />,
  trending: <TrendingUp size={16} />,
  landmark: <Landmark size={16} />,
  candlestick: <CandlestickChart size={16} />,
  users: <Users size={16} />,
};

const CHIPS = ["日K行情", "财经快讯", "公告检索", "财务摘要", "事件研究 CAR", "宏观指标", "龙虎榜", "融资融券", "研报评级"];

/** 热门 A 股池：每次点击 chip 时随机抽一只，让示例 prompt 不刻板 */
const STOCK_POOL: { code: string; name: string }[] = [
  { code: "600519", name: "贵州茅台" },
  { code: "000858", name: "五粮液" },
  { code: "300750", name: "宁德时代" },
  { code: "600036", name: "招商银行" },
  { code: "002594", name: "比亚迪" },
  { code: "601318", name: "中国平安" },
  { code: "000333", name: "美的集团" },
  { code: "600276", name: "恒瑞医药" },
  { code: "601012", name: "隆基绿能" },
  { code: "688981", name: "中芯国际" },
  { code: "601899", name: "紫金矿业" },
  { code: "600030", name: "中信证券" },
  { code: "000651", name: "格力电器" },
  { code: "002475", name: "立讯精密" },
  { code: "601138", name: "工业富联" },
];
const pickStock = () => STOCK_POOL[Math.floor(Math.random() * STOCK_POOL.length)];

/** chip 标签 → 每次点击返回一个具体 prompt 字符串（标的随机抽，宏观类无标的） */
const CHIP_PROMPTS: Record<string, () => string> = {
  "日K行情": () => { const s = pickStock(); return `用日K行情分析 ${s.code}（${s.name}）的近期走势`; },
  "财经快讯": () => "看下最近的重要财经快讯",
  "公告检索": () => { const s = pickStock(); return `检索 ${s.code}（${s.name}）的最新公告`; },
  "财务摘要": () => { const s = pickStock(); return `查一下 ${s.code}（${s.name}）的财务摘要`; },
  "事件研究 CAR": () => { const s = pickStock(); return `对 ${s.code}（${s.name}）做一次事件研究 CAR 分析`; },
  "宏观指标": () => "看下最新宏观指标（CPI / PMI / M2）",
  "龙虎榜": () => "看下最新龙虎榜",
  "融资融券": () => { const s = pickStock(); return `查 ${s.code}（${s.name}）的融资融券数据`; },
  "研报评级": () => { const s = pickStock(); return `查 ${s.code}（${s.name}）的最新研报评级`; },
};

/** 空态 hero */
function Hero() {
  const sendMessage = useStore((s) => s.sendMessage);
  const setMode = useStore((s) => s.setMode);
  const streaming = useStore((s) => s.streaming);
  const setPromptSeed = useStore((s) => s.setPromptSeed);

  // 6 条推荐（2 快速 + 2 专家 + 2 团队），纯静态池里随机抽
  const [suggestions, setSuggestions] = useState<Suggestion[]>(pickSix);
  // 每次点击换一批时切换的「翻页标记」，仅用于触发图标旋转动画
  const [refreshing, setRefreshing] = useState(false);

  const refresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      const res = await api.suggestions();
      if (Array.isArray(res.items) && res.items.length > 0) {
        setSuggestions(res.items);
      } else {
        setSuggestions(pickSix());
      }
    } catch {
      setSuggestions(pickSix());
    } finally {
      // 让旋转动画跑完一圈后复位
      window.setTimeout(() => setRefreshing(false), 600);
    }
  };

  return (
    <div className="flex min-h-full flex-col items-center justify-center px-6 py-10">
      <div className="w-full max-w-2xl animate-fadeUp">
        <div className="flex items-center gap-2 text-[11.5px] font-medium tracking-widest text-jade">
          <Sparkles size={13} />
          FIN EVENT RESEARCH WORKBENCH
        </div>
        <h2 className="mt-3 font-serif text-[34px] font-bold leading-tight text-ink">
          Hunt events. <span className="text-brand">Trace echoes.</span>
        </h2>
        <p className="mt-3 max-w-xl text-[13.5px] leading-[1.9] text-mute">
          对话式 AI 金融事件分析工作台：提问即研究。主理人 Agent 调用 akshare 真实数据技能，
          流式输出结论，并把 K 线、曲线、数据表、证据与研究报告沉淀为可回看的产出物。
          深度问题可切换「研究团队」模式，多专家并行作业、复核员把关。
        </p>

        {/* 建议问题 */}
        <div className="mt-7 grid grid-cols-1 gap-2.5 sm:grid-cols-2">
          {suggestions.map((s, i) => (
            <button
              key={`${suggestionPrompt(s)}-${i}`}
              disabled={streaming}
              onClick={() => {
                setMode(s.mode);
                void sendMessage(suggestionPrompt(s), s.mode, s.agent);
              }}
              className="group flex items-start gap-3 rounded-card border border-edge bg-card px-4 py-3.5 text-left shadow-card transition-all duration-200 hover:-translate-y-0.5 hover:border-brand/40 hover:shadow-pop disabled:opacity-50"
            >
              <span className="flex shrink-0 flex-col items-center gap-1">
                <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-soft text-brand transition-colors group-hover:bg-brand group-hover:text-card">
                  {ICON_BY_HINT[s.icon_hint] ?? <Newspaper size={16} />}
                </span>
                {s.mode === "team" && (
                  <span className="rounded bg-jade-soft px-1.5 py-px text-[10px] font-semibold text-jade">团队</span>
                )}
              </span>
              <span className="min-w-0">
                <span className="block text-[13.5px] font-medium leading-snug text-ink">
                  {suggestionPrompt(s)}
                </span>
                <span className="mt-1 block text-[11.5px] text-faint">{s.desc}</span>
              </span>
            </button>
          ))}
        </div>

        {/* 换一批：放在建议问题下方居中 */}
        <div className="mt-4 flex justify-center">
          <button
            type="button"
            onClick={refresh}
            disabled={refreshing || streaming}
            className="flex items-center gap-1 rounded-full border border-edge bg-card px-2.5 py-1 text-[11px] text-mute transition-colors hover:border-brand/40 hover:text-brand disabled:opacity-50"
            title="根据当前热点事件刷新推荐"
          >
            <RefreshIcon spinning={refreshing} />
            换一批
          </button>
        </div>

        {/* 能力 chips */}
        <div className="mt-6 flex flex-wrap items-center gap-1.5">
          <LineChart size={13} className="text-faint" />
          {CHIPS.map((c) => (
            <button
              key={c}
              type="button"
              title="点击填入示例 prompt 到输入框"
              onClick={() => setPromptSeed((CHIP_PROMPTS[c] ?? (() => c))())}
              className="rounded-full border border-edge bg-card px-2.5 py-1 text-[11.5px] text-mute transition-all hover:border-jade/40 hover:bg-jade-soft hover:text-jade active:scale-95"
            >
              {c}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function RefreshIcon({ spinning }: { spinning: boolean }) {
  return (
    <svg
      width="11"
      height="11"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={spinning ? "animate-spin" : ""}
    >
      <polyline points="23 4 23 10 17 10" />
      <polyline points="1 20 1 14 7 14" />
      <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
    </svg>
  );
}

/** 加载历史时的骨架屏 */
function Skeleton() {
  return (
    <div className="mx-auto w-full max-w-3xl space-y-4 px-6 py-8">
      {[0.55, 0.8, 0.4].map((w, i) => (
        <div key={i} className={i % 2 === 0 ? "flex justify-end" : "flex"}>
          <div
            className="h-16 rounded-card border border-edge bg-gradient-to-r from-[#F1EFEB] via-[#FAF9F7] to-[#F1EFEB] bg-[length:400px_100%] animate-shimmer"
            style={{ width: `${w * 100}%` }}
          />
        </div>
      ))}
    </div>
  );
}

export default function ChatPanel() {
  const messages = useStore((s) => s.messages);
  const loadingCase = useStore((s) => s.loadingCase);
  const currentCaseId = useStore((s) => s.currentCaseId);
  const streaming = useStore((s) => s.streaming);

  const scrollRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  // 流式期间自动吸底（用户上翻则暂停吸底）
  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [messages, streaming]);

  useEffect(() => {
    stickRef.current = true;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [currentCaseId]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  };

  const empty = messages.length === 0 && !loadingCase;

  return (
    <section className="flex h-full min-w-0 flex-1 flex-col bg-paper">
      <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto">
        {empty ? (
          <Hero />
        ) : loadingCase ? (
          <Skeleton />
        ) : (
          <div className="mx-auto w-full max-w-3xl space-y-6 px-6 py-7">
            {messages.map((m) => (
              <MessageItem key={m.id} message={m} />
            ))}
            <div className="h-2" />
          </div>
        )}
      </div>
      <Composer />
    </section>
  );
}
