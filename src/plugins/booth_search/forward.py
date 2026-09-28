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


async def _image_segment(url: str | None, sem: asyncio.Semaphore):
    if not url:
        return None
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
                             nickname: str = "Booth 搜索") -> list:
    """构建转发节点：首个节点为摘要（查询/关键词/告警），其后每个商品一节点（图+文字）。"""
    sem = asyncio.Semaphore(3)
    top = entries[:max_n]
    imgs = await asyncio.gather(
        *[_image_segment(entry_image_url(it), sem) for it in top])

    head_segs = [MessageSegment.text(header)]
    for note in notes:
        head_segs.append(MessageSegment.text("\n" + note))
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
        if flags:
            lines.append("[" + " | ".join(flags) + "]")
        shop = (it.get("shop") or {}).get("name")
        if shop:
            cat = f" · {it['category']}" if it.get("category") else ""
            lines.append(f"店铺: {shop}{cat}")
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
