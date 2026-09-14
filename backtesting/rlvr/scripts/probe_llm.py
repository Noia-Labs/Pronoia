import os, time, asyncio
os.environ.setdefault("PRONOIA_BT_FAST", "0")
from app import config
from openai import AsyncOpenAI

k = config.LLM_API_KEY
m = "deepseek-v4-flash"


async def probe(name, base):
    c = AsyncOpenAI(base_url=base, api_key=k)
    t = time.time()
    try:
        r = await c.chat.completions.create(
            model=m, messages=[{"role": "user", "content": "say ok"}], max_tokens=8
        )
        print(f"{name}: {round(time.time()-t,1)}s -> {r.choices[0].message.content!r}", flush=True)
    except Exception as e:
        print(f"{name}: {round(time.time()-t,1)}s -> ERR {type(e).__name__}: {str(e)[:100]}", flush=True)


async def main():
    await probe("plan  ", "https://ark.cn-beijing.volces.com/api/plan/v3")
    await probe("coding", "https://ark.cn-beijing.volces.com/api/coding/v3")
    await probe("plan  ", "https://ark.cn-beijing.volces.com/api/plan/v3")


asyncio.run(main())