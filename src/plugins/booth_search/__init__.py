"""booth_search 插件：/vrc search <关键词> 或 /vrc search + 图片。

图片流程：识图 AI 提取关键词（可配置，无 key 自动跳过）→ booth imgsearch 反查 +
关键词搜索合并。文本流程：直接关键词搜索。
"""
import os
import re
import tempfile
from pathlib import Path

import httpx
from nonebot import get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.rule import Rule

from . import booth_client, vision
from .config import Config
from .format import format_results

plugin_config = get_plugin_config(Config)

SEARCH_RE = re.compile(r"^[/！!]?vrc\s*search(?:\s+(.*))?$", re.I | re.S)
USAGE = ("用法:\n/vrc search <关键词>   —— Booth 商品搜索\n"
         "/vrc search + 图片     —— 以图搜图（可附文字提示）")


def _is_search(event) -> bool:
    return bool(SEARCH_RE.match(event.get_plaintext().strip()))


matcher = on_message(Rule(_is_search), priority=10, block=True)


def _collect_image_urls(event: MessageEvent) -> list:
    urls = [seg.data.get("url") for seg in event.get_message()
            if seg.type == "image" and seg.data.get("url")]
    if not urls and getattr(event, "reply", None) is not None:
        reply_msg = getattr(event.reply, "message", None)
        if reply_msg is not None:
            urls = [seg.data.get("url") for seg in reply_msg
                    if seg.type == "image" and seg.data.get("url")]
    return urls


async def _download_image(url: str) -> bytes:
    """下载 QQ 消息里的图片（QQ 多媒体域名 + booth 图床都需要的话带上 Referer）。"""
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://booth.pm/"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        if len(resp.content) < 100:
            raise booth_client.BoothCliError("图片下载内容异常（过短）")
        return resp.content


async def _handle_image(image_url: str, hint: str) -> str:
    cfg = plugin_config
    image_bytes = await _download_image(image_url)

    # 1) 识图 AI 提词（未配 key 则跳过，仅靠 CLI 自带派生词）
    keywords, item_type = [], ""
    if cfg.vision_api_key:
        try:
            keywords, item_type = await vision.extract_keywords(
                image_bytes, hint=hint, base_url=cfg.vision_base_url,
                api_key=cfg.vision_api_key, model=cfg.vision_model,
                timeout=cfg.vision_timeout)
            logger.info(f"识图关键词: {keywords} (type={item_type})")
        except Exception as e:
            logger.warning(f"识图 AI 失败（退化为纯图搜）: {e}")

    # 2) CLI 反向图搜
    matches = []
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tf:
            tf.write(image_bytes)
            tmp_path = tf.name
        data = booth_client.imgsearch(
            tmp_path, headless=cfg.imgsearch_headless,
            limit=cfg.booth_limit, timeout=cfg.imgsearch_timeout)
        matches = data.get("matches") or []
        if data.get("derived_query") and data["derived_query"] not in keywords:
            keywords.append(data["derived_query"])
    except booth_client.BoothCliError as e:
        logger.warning(f"图搜失败: {e}")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # 3) 关键词搜索合并（图搜候选优先）
    seen = {m.get("id") for m in matches}
    merged = list(matches)
    for kw in keywords[:2]:
        try:
            res = booth_client.search(kw, limit=cfg.booth_limit,
                                      sort=cfg.booth_sort, adult=cfg.r18_mode,
                                      cli_path=cfg.booth_cli_path,
                                      timeout=cfg.search_timeout)
        except booth_client.BoothCliError as e:
            logger.warning(f"关键词搜索失败({kw}): {e}")
            continue
        for it in res.get("items") or []:
            if it["id"] not in seen:
                it["via"] = "关键词"
                seen.add(it["id"])
                merged.append(it)

    if not merged:
        return ("没找到相关 Booth 商品。识别关键词: "
                + (" / ".join(keywords[:5]) or "（无）")
                + "\n可尝试补充文字提示重发：/vrc search <提示> + 图片")
    title = "识图关键词: " + (" / ".join(keywords[:5]) or "（无）") if keywords else "图搜结果:"
    return format_results(merged, max_n=cfg.booth_limit, title=title)


def _handle_text_sync(hint: str) -> str:
    cfg = plugin_config
    res = booth_client.search(hint, limit=cfg.booth_limit, sort=cfg.booth_sort,
                              adult=cfg.r18_mode, cli_path=cfg.booth_cli_path,
                              timeout=cfg.search_timeout)
    items = res.get("items") or []
    if not items:
        return f"Booth 上没搜到「{hint}」（共 {res.get('total') or 0} 件）"
    total = f"共 {res['total']:,} 件，显示前 {len(items)}:" if res.get("total") else f"前 {len(items)}:"
    for it in items:
        it.setdefault("via", "")
    return format_results(items, max_n=cfg.booth_limit, title=total)


@matcher.handle()
async def handle_vrc_search(bot, event: MessageEvent):
    m = SEARCH_RE.match(event.get_plaintext().strip())
    hint = (m.group(1) or "").strip() if m else ""
    urls = _collect_image_urls(event)

    if not urls and not hint:
        await matcher.finish(USAGE)

    try:
        if urls:
            if not hint:
                await matcher.send("收到图片，正在反查 Booth（10-60 秒）…")
            else:
                await matcher.send("收到图片+提示，正在反查 Booth（10-60 秒）…")
            text = await _handle_image(urls[0], hint)
        else:
            text = _handle_text_sync(hint)
    except booth_client.BoothCliError as e:
        text = f"搜索失败: {e}"
    except Exception as e:
        logger.exception("vrc search 未捕获异常")
        text = f"内部错误: {type(e).__name__}: {e}"
    await matcher.finish(text)
