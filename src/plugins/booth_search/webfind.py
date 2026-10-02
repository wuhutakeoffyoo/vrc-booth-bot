"""网络检索兜底：站内搜索无果时，从公开搜索引擎找 booth.pm 商品链接。

- ddg：DuckDuckGo HTML 版（免 key；数据中心 IP 偶尔被挑战，失败返回空）
- api：用户选择的检索服务，由共享 search_api 适配协议；旧 EXA_* 继续有效

仅返回 booth.pm / *.booth.pm 的商品链接与 ID。无外部依赖（httpx）。
"""
import html
import asyncio
import re
import urllib.parse

import httpx
try:
    from . import search_api
except ImportError:
    import search_api

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"}


def item_ids_from_urls(urls: list) -> list:
    """从 URL 列表按出现顺序提取商品 ID（去重）。"""
    return search_api.item_ids_from_urls(urls)


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


async def api_find(keywords: str, api_key: str = "", timeout: int = 15,
                   base_url: str = "", provider: str = "auto") -> list:
    """用户选择的检索 API；失败保留其他检索结果。"""
    try:
        wire = await asyncio.to_thread(search_api.prepare, keywords, base_url, api_key, provider)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream(wire.method, wire.url, content=wire.body,
                                     headers=wire.headers) as resp:
                resp.raise_for_status()
                raw = bytearray()
                async for chunk in resp.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > search_api.MAX_RESPONSE_BYTES:
                        return []
                return search_api.parse_results(wire, raw)
    except Exception:
        return []


async def exa_find(keywords: str, api_key: str, timeout: int = 15,
                   base_url: str = "https://api.exa.ai") -> list:
    """兼容旧调用；新配置使用 SEARCH_*。"""
    return await api_find(keywords, api_key, timeout, base_url, "exa")


async def find_booth_item_ids(keywords: str, *, exa_api_key: str = "",
                              exa_base_url: str = "https://api.exa.ai",
                              search_api_key: str = "", search_base_url: str = "",
                              search_provider: str = "auto", ddg_enabled: bool = True,
                              timeout: int = 15) -> list:
    """新搜索配置优先；未配置时兼容旧 Exa，不复用旧服务的 key。"""
    ids = await ddg_find(keywords, timeout=timeout) if ddg_enabled else []
    extra = []
    if search_base_url:
        extra = await api_find(keywords, search_api_key, timeout, search_base_url, search_provider)
    elif exa_api_key:
        extra = await exa_find(keywords, exa_api_key, timeout, exa_base_url or "https://api.exa.ai")
    for iid in extra:
        if iid not in ids:
            ids.append(iid)
    return ids
