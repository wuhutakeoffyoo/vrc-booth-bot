"""网络检索兜底：站内搜索无果时，从公开搜索引擎找 booth.pm 商品链接。

- ddg：DuckDuckGo HTML 版（免 key；数据中心 IP 偶尔被挑战，失败返回空）
- exa：Exa AI 搜索 API（可选，配 EXA_API_KEY 时启用，支持 includeDomains）

仅返回 booth.pm / *.booth.pm 的商品链接与 ID。无外部依赖（httpx）。
"""
import html
import asyncio
import re
import urllib.parse

import httpx
try:
    from . import provider_api
except ImportError:
    import provider_api

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"}


def item_ids_from_urls(urls: list) -> list:
    """从 URL 列表按出现顺序提取商品 ID（去重）。"""
    ids = []
    for u in urls:
        try:
            parts = urllib.parse.urlsplit(u)
            host = (parts.hostname or "").lower()
            if (parts.scheme not in ("http", "https") or parts.username or parts.password
                    or (host != "booth.pm" and not host.endswith(".booth.pm"))):
                continue
            m = re.fullmatch(r"/(?:[a-z]{2}/)?items/(\d+)/?", parts.path)
        except ValueError:
            continue
        if m:
            iid = int(m.group(1))
            if iid not in ids:
                ids.append(iid)
    return ids


def ids_from_ddg_html(content: str) -> list:
    """解析 DDG HTML 结果页里的 booth 商品链接。"""
    urls = []
    for m in re.finditer(r'href="([^"]+)"', content or ""):
        href = html.unescape(m.group(1))
        try:
            parts = urllib.parse.urlsplit(href)
            host = (parts.hostname or "").lower()
            if host == "duckduckgo.com" or host.endswith(".duckduckgo.com"):
                href = urllib.parse.parse_qs(parts.query).get("uddg", [href])[0]
        except ValueError:
            continue
        urls.append(href)
    return item_ids_from_urls(urls)


async def ddg_find(keywords: str, timeout: int = 15) -> list:
    """DuckDuckGo HTML 检索 <keywords> booth.pm，返回商品 ID 列表。"""
    q = f"{keywords} booth.pm"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers=_HEADERS) as client:
            resp = await client.post("https://html.duckduckgo.com/html/",
                                     data={"q": q})
            resp.raise_for_status()
            return ids_from_ddg_html(resp.text)
    except Exception:
        return []


async def exa_find(keywords: str, api_key: str, timeout: int = 15,
                   base_url: str = "https://api.exa.ai") -> list:
    """Exa AI 搜索（可选）：限定 booth.pm 域名，返回商品 ID 列表。"""
    try:
        url = await asyncio.to_thread(provider_api.exa_url, base_url)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.post(
                url,
                json={"query": keywords, "numResults": 8,
                      "includeDomains": ["booth.pm"]},
                headers={"x-api-key": api_key, "Content-Type": "application/json"})
            resp.raise_for_status()
            data = resp.json()
            urls = [r.get("url", "") for r in data.get("results", [])]
            return item_ids_from_urls(urls)
    except Exception:
        return []


async def find_booth_item_ids(keywords: str, *, exa_api_key: str = "",
                              exa_base_url: str = "https://api.exa.ai",
                              timeout: int = 15) -> list:
    """网络检索兜底入口：DDG 优先，Exa 补充（配了 key 时），合并去重。"""
    ids = await ddg_find(keywords, timeout=timeout)
    if exa_api_key:
        for iid in await exa_find(keywords, api_key=exa_api_key, timeout=timeout, base_url=exa_base_url):
            if iid not in ids:
                ids.append(iid)
    return ids
