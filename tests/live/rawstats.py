"""从 astrbot-local 容器内直连 Docker Engine API，取 astrbot-local 的内存明细。

用途：定位 container_memory 探针"疑似泄漏"误报的根因 —— 确认 effective 指标
（usage - stats.cache）为何在相邻两次巡检间出现 ±110MB 锯齿。

运行：docker cp 进容器后 `python /tmp/rawstats.py`
"""
import asyncio

import aiohttp

MB = 1048576.0


async def one(sock: str = "/var/run/docker.sock") -> dict:
    conn = aiohttp.UnixConnector(path=sock)
    async with aiohttp.ClientSession(connector=conn) as s:
        async with s.get(
            "http://localhost/containers/astrbot-local/stats",
            params={"stream": "false"},
        ) as r:
            return await r.json()


def dump(tag: str, d: dict) -> None:
    m = d.get("memory_stats") or {}
    st = m.get("stats") or {}
    usage = (m.get("usage") or 0) / MB
    cache = (st.get("cache") or 0) / MB
    file_ = (st.get("file") or 0) / MB
    print(f"[{tag}] usage={usage:7.1f}  cache={cache:7.1f}  file={file_:7.1f}  "
          f"rss={(st.get('rss') or 0) / MB:7.1f}  anon={(st.get('anon') or 0) / MB:7.1f}  "
          f"inactive_file={(st.get('inactive_file') or 0) / MB:7.1f}  "
          f"active_file={(st.get('active_file') or 0) / MB:7.1f}")
    print(f"[{tag}] -> usage-cache = {usage - cache:7.1f} MB   (探针当前口径)")
    print(f"[{tag}] -> usage-file  = {usage - file_:7.1f} MB")
    if not st:
        print(f"[{tag}] !! stats 为空，raw keys = {sorted(st.keys())}")


async def main() -> None:
    for i in range(3):
        dump(f"t{i}", await one())
        if i < 2:
            await asyncio.sleep(70)


asyncio.run(main())
