#!/usr/bin/env python3
"""E2E 流测试：通过 vite(27532) 走与浏览器完全相同的路径。

用法:
  python3 scripts/test_stream_e2e.py normal    # 普通对话（走 LLM）
  python3 scripts/test_stream_e2e.py cut 8    # 断流测试：8 秒后掐断连接，然后查服务端落库
"""
import json
import sys
import time
import urllib.request

VITE = "http://127.0.0.1:27532"


def post_chat_stream(question: str, cut_after: float | None = None):
    """POST /api/chat 并读 SSE 流，返回 (事件列表, 是否被掐断)。"""
    body = json.dumps({"message": question, "mode": "agent", "agent": "router"}).encode()
    req = urllib.request.Request(
        f"{VITE}/api/chat", data=body,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    events = []
    cut = False
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            ttfb = time.time() - start
            print(f"  TTFB={ttfb:.2f}s status={resp.status}")
            buf = b""
            while True:
                if cut_after is not None and time.time() - start > cut_after:
                    raise TimeoutError("planned cut")
                chunk = resp.read1(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n\n" in buf:
                    raw, buf = buf.split(b"\n\n", 1)
                    line = raw.decode(errors="replace").strip()
                    if line.startswith("data: "):
                        try:
                            events.append(json.loads(line[6:]))
                        except json.JSONDecodeError:
                            pass
    except TimeoutError:
        cut = True
    return events, cut


def post_chat_stream2(question: str, cut_after_content: float):
    """正文 token 出现后再等 N 秒掐断，模拟"内容已部分流出后被掐"。"""
    body = json.dumps({"message": question, "mode": "agent", "agent": "router"}).encode()
    req = urllib.request.Request(
        f"{VITE}/api/chat", data=body,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    events = []
    buf = b""
    content_start = None
    cut = False
    with urllib.request.urlopen(req, timeout=600) as resp:
        while True:
            now = time.time()
            if content_start is not None and now - content_start > cut_after_content:
                cut = True
                break
            chunk = resp.read1(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                raw, buf = buf.split(b"\n\n", 1)
                line = raw.decode(errors="replace").strip()
                if line.startswith("data: "):
                    try:
                        e = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    events.append(e)
                    if e.get("type") == "token" and content_start is None:
                        content_start = time.time()
                        print(f"  正文 token 出现于 {content_start:.1f}s（epoch）")
    return events, cut


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    if mode == "normal":
        q = sys.argv[2] if len(sys.argv) > 2 else "用一句话说明什么是多因子量化选股。"
        print(f"[normal] question: {q}")
        t0 = time.time()
        events, _ = post_chat_stream(q)
        dt = time.time() - t0
        types = {}
        for e in events:
            types[e.get("type", "?")] = types.get(e.get("type", "?"), 0) + 1
        done = [e for e in events if e.get("type") in ("done", "error")]
        content_len = sum(len(e.get("delta", "")) for e in events if e.get("type") == "token")
        case_id = (events[0].get("case_id") if events else None) or (done[0].get("case_id") if done else None)
        print(f"  events={len(events)} types={types}")
        print(f"  token_chars={content_len} duration={dt:.1f}s")
        print(f"  final={[{'type': d.get('type'), 'msg': str(d)[:120]} for d in done]}")
        print(f"  case_id={case_id}")
        rc = 0 if done and done[0].get("type") == "done" and content_len > 0 else 1
        print(f"[normal] {'PASS' if rc == 0 else 'FAIL'}")
        sys.exit(rc)
    elif mode == "cut":
        cut_after = float(sys.argv[2]) if len(sys.argv) > 2 else 8
        q = "深入研究新能源汽车行业2026年的投资逻辑，给出分析框架。"  # 会触发技能调用的长任务
        print(f"[cut] question: {q[:30]}... cut_after={cut_after}s")
        t0 = time.time()
        events, cut = post_chat_stream(q, cut_after=cut_after)
        print(f"  streamed {len(events)} events in {time.time()-t0:.1f}s, cut={cut}")
        case_id = next((e.get("case_id") for e in events if e.get("case_id")), None)
        if not case_id:
            print("[cut] FAIL: no case_id seen in stream")
            sys.exit(1)
        print(f"  case_id={case_id}, 等待 5s 后查服务端落库 ...")
        time.sleep(5)
        detail = json.loads(urllib.request.urlopen(f"{VITE}/api/cases/{case_id}", timeout=30).read())
        msgs = detail.get("messages", [])
        print(f"  服务端消息数={len(msgs)}")
        for m in msgs[-3:]:
            c = m.get("content") or ""
            print(f"    [{m.get('role')}] len={len(c)} preview={c[:60]!r}")
        last = msgs[-1] if msgs else None
        # 断流挽回判据：最后一条 assistant 有内容
        ok = last and last.get("role") == "assistant" and (last.get("content") or "").strip()
        print(f"[cut] {'PASS (可挽回)' if ok else 'INFO (无可挽回内容，属正常早期断流)'}")
        sys.exit(0)
    elif mode == "late_cut":
        cut_after = float(sys.argv[2]) if len(sys.argv) > 2 else 5
        q = "深入研究新能源汽车行业2026年的投资逻辑，给出完整的分析框架和关键变量。"
        print(f"[late_cut] 等正文流出 {cut_after}s 后掐断")
        events, cut = post_chat_stream2(q, cut_after)
        n_token = sum(1 for e in events if e.get("type") == "token")
        case_id = next((e.get("case_id") for e in events if e.get("case_id")), None)
        print(f"  掐断={cut} 已收 token 事件={n_token} case={case_id}")
        time.sleep(3)
        detail = json.loads(urllib.request.urlopen(f"{VITE}/api/cases/{case_id}", timeout=30).read())
        msgs = detail.get("messages", [])
        last, prev = msgs[-1], msgs[-2]
        ok = (last.get("role") == "assistant" and (last.get("content") or "").strip()
              and prev.get("role") == "user" and prev.get("content") == q)
        print(f"  服务端 last.assistant content_len={len(last.get('content') or '')}")
        print(f"  prev 匹配本轮问题: {prev.get('content') == q}")
        print(f"[late_cut] {'PASS (挽回路径可命中)' if ok else 'FAIL'}")
        sys.exit(0 if ok else 1)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
