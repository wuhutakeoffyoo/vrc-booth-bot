"""QQ 合并转发消息（OneBot v11 forward/node）构建与发送。

商品图以 base64 内嵌：pximg 需要 Referer，QQ 侧直接拉 URL 会失败，
统一由 bot 下载后转 base64。图片下载失败时该节点降级为纯文本。
"""
import asyncio
import base64

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import Message, MessageSegment

_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://booth.pm/"}


def entry_image_url(it: dict) -> str | None:
    return it.get("image") or ((it.get("images") or [None])[0])


def _thumb_url(url: str | None) -> str | None:
    """pximg 原图转 300x300 缩略图（原图多 MB 会导致 base64 发送失败/极慢）。"""
    if not url:
        return url
    if ("booth.pximg.net" in url and url.endswith(".jpg")
            and "/c/" not in url):
        stem = url[:-4]
        return stem.replace("booth.pximg.net/", "booth.pximg.net/c/300x300_a2_g5/", 1) \
            + "_base_resized.jpg"
    return url


async def _image_segment(url: str | None, sem: asyncio.Semaphore):
    if not url:
        return None
    url = _thumb_url(url)
    async with sem:
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                    resp = await client.get(url, headers=_HEADERS)
                    resp.raise_for_status()
                    if len(resp.content) >= 100:
                        b64 = base64.b64encode(resp.content).decode()
                        return MessageSegment.image("base64://" + b64)
            except Exception as e:
                logger.debug(f"转发图片下载失败({url[:60]}): {e}")
                if attempt == 1:
                    return None
                await asyncio.sleep(1)
    return None


def _node(self_id: int, nickname: str, segments: list) -> dict:
    return {"type": "node",
            "data": {"user_id": self_id, "nickname": nickname,
                     "content": Message(segments)}}


async def build_result_nodes(self_id: int, header: str, notes: list,
                             entries: list, max_n: int = 6,
                             nickname: str = "Booth 搜索",
                             page: int = 1, query_hint: str = "",
                             total: int | None = None) -> list:
    """构建转发节点：摘要节点（含翻页提示）+ 每商品一节点（图+文字）。"""
    sem = asyncio.Semaphore(3)
    top = entries[:max_n]
    imgs = await asyncio.gather(
        *[_image_segment(entry_image_url(it), sem) for it in top])

    head_lines = [header]
    for note in notes:
        head_lines.append(note)
    # 翻页提示：还有更多结果时提示页码指令
    if total and total > page * max_n:
        if query_hint:
            head_lines.append(f"翻下一页：/vrc search {query_hint} {page + 1}")
        else:
            head_lines.append(f"翻下一页：/vrc search <关键词> {page + 1}")
    head_segs = [MessageSegment.text("\n".join(head_lines))]
    nodes = [_node(self_id, nickname, head_segs)]

    from .format import price_str
    for i, (it, img) in enumerate(zip(top, imgs), 1):
        flags = []
        if it.get("via"):
            flags.append(it["via"])
        if it.get("is_adult"):
            flags.append("R-18")
        if it.get("is_sold_out"):
            flags.append("已售罄")
        lines = [f"{i}. {it.get('name') or '(标题未知)'}  {price_str(it.get('price'))}"]
        meta = []
        wl = it.get("wish_lists_count")
        if isinstance(wl, int) and wl > 0:
            meta.append(f"♥ {wl:,} 收藏")
        pub = str(it.get("published_at") or "")[:10]
        if pub:
            meta.append(f"上架 {pub}")
        if flags:
            meta.append("[" + " | ".join(flags) + "]")
        if meta:
            lines.append("  " + "  ".join(meta))
        shop = (it.get("shop") or {}).get("name")
        if shop:
            cat = f" · {it['category']}" if it.get("category") else ""
            lines.append(f"店铺: {shop}{cat}")
        tags = [t for t in (it.get("tags") or []) if t][:5]
        if tags:
            lines.append(" ".join("#" + str(t) for t in tags))
        url = it.get("url") or f"https://booth.pm/ja/items/{it.get('id')}"
        lines.append(url)
        segs = ([img] if img is not None else []) + [MessageSegment.text("\n".join(lines))]
        nodes.append(_node(self_id, nickname, segs))
    return nodes


async def send(bot, event, nodes: list) -> bool:
    """发送合并转发；群聊/私聊自动分派。失败返回 False（调用方回退纯文本）。"""
    try:
        gid = getattr(event, "group_id", None)
        if gid is not None:
            await bot.call_api("send_group_forward_msg",
                               group_id=gid, messages=nodes)
        else:
            await bot.call_api("send_private_forward_msg",
                               user_id=event.user_id, messages=nodes)
        return True
    except Exception as e:
        logger.warning(f"合并转发发送失败，回退纯文本: {e}")
        return False
