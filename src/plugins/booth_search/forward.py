"""QQ 合并转发消息（OneBot v11 forward/node）构建与发送。

商品图以 base64 内嵌：pximg 需要 Referer，QQ 侧直接拉 URL 会失败，
统一由 bot 下载后转 base64。图片下载失败时该节点降级为纯文本。
"""
import asyncio
import base64

from .image_download import DEFAULT_HOSTS, download_image
from nonebot import logger
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError

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


async def _image_segment(url: str | None, sem: asyncio.Semaphore, allowed_hosts=DEFAULT_HOSTS):
    if not url:
        return None
    url = _thumb_url(url)
    async with sem:
        for attempt in range(2):
            try:
                content = await download_image(url, allowed_hosts=allowed_hosts, timeout=20)
                b64 = base64.b64encode(content).decode()
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
                             total: int | None = None, command: str = "search",
                             has_next: bool | None = None, allowed_hosts=DEFAULT_HOSTS) -> list:
    """构建转发节点：摘要节点（含翻页提示）+ 每商品一节点（图+文字）。"""
    sem = asyncio.Semaphore(3)
    top = entries[:max_n]
    imgs = await asyncio.gather(
        *[_image_segment(entry_image_url(it), sem, allowed_hosts) for it in top])

    head_lines = [header]
    for note in notes:
        head_lines.append(note)
    # 翻页提示：还有更多结果时提示页码指令
    if has_next if has_next is not None else (total and total > page * max_n):
        command = "r18" if command == "r18" else "search"
        if query_hint:
            head_lines.append(f"翻下一页：/vrc {command} {query_hint} {page + 1}")
        else:
            head_lines.append(f"翻下一页：/vrc {command} <关键词> {page + 1}")
    head_segs = [MessageSegment.text("\n".join(head_lines))]
    nodes = [_node(self_id, nickname, head_segs)]

    from .format import price_str
    from .search_evidence import evidence_label
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
        if evidence_label(it):
            lines.append(evidence_label(it))
        segs = ([img] if img is not None else []) + [MessageSegment.text("\n".join(lines))]
        nodes.append(_node(self_id, nickname, segs))
    return nodes


async def send(bot, event, nodes: list) -> bool:
    """发送合并转发；群聊/私聊自动分派。返回 False 时调用方回退纯文本。

    超时/网络类异常按「可能已送达」处理返回 True：大体积转发（多节点 base64 图）
    客户端超时后 NapCat 往往仍完成投递，此时回退纯文本会发出第二条重复消息——
    宁缺勿重。只有 NapCat 明确拒绝（ActionFailed）或其它确定失败才回退文字版。
    """
    try:
        gid = getattr(event, "group_id", None)
        if gid is not None:
            await bot.call_api("send_group_forward_msg",
                               group_id=gid, messages=nodes)
        else:
            await bot.call_api("send_private_forward_msg",
                               user_id=event.user_id, messages=nodes)
        return True
    except NetworkError as e:
        logger.warning(f"合并转发网络异常（可能已送达，不回退纯文本防重复）: {e}")
        return True
    except ActionFailed as e:
        logger.warning(f"合并转发被拒绝（ActionFailed），回退纯文本: {e}")
        return False
    except Exception as e:
        lowered = f"{type(e).__name__} {e}".lower()
        if "timeout" in lowered or "timed out" in lowered:
            logger.warning(f"合并转发超时（可能已送达，不回退纯文本防重复）: {e}")
            return True
        logger.warning(f"合并转发发送失败，回退纯文本: {e}")
        return False
