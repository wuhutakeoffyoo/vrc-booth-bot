"""booth_search 插件：/vrc search <关键词> 或 /vrc search + 图片。

访问控制（逻辑在 access.py）：
- 群白名单 GROUP_WHITELIST 非空时，仅白名单群响应（白名单外静默忽略）；
- 私聊：管理员始终可用；非管理员由 ALLOW_PRIVATE 决定；
- 敏感指令 /vrc r18（R-18 专项搜索）仅管理员可用。

图片流程：识图 AI 提取关键词（可配置，无 key 自动跳过）→ booth imgsearch 反查 +
关键词搜索合并。文本流程：直接关键词搜索。
"""
import asyncio
import os
import re
import tempfile
from pathlib import Path

import httpx
from nonebot import get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.rule import Rule

from . import access, booth_client, vision
from .config import Config
from .format import format_results

plugin_config = get_plugin_config(Config)

SEARCH_RE = re.compile(r"^[/！!]?vrc\s*search(?:\s+(.*))?$", re.I | re.S)
R18_RE = re.compile(r"^[/！!]?vrc\s*r18(?:\s+(.*))?$", re.I | re.S)
USAGE = ("用法:\n/vrc search <关键词>   —— Booth 商品搜索\n"
         "/vrc search + 图片     —— 以图搜图（可附文字提示）")
DENY_MSG = "该指令仅对管理员开放"


def _plain(event) -> str:
    try:
        return event.get_plaintext().strip()
    except Exception:
        return ""


def _is_admin(event) -> bool:
    return access.is_admin(plugin_config, getattr(event, "user_id", ""))


def _access_ok(event) -> bool:
    return access.access_ok(plugin_config,
                            user_id=getattr(event, "user_id", ""),
                            group_id=getattr(event, "group_id", None))


def _is_search(event) -> bool:
    return bool(SEARCH_RE.match(_plain(event)))


def _is_r18(event) -> bool:
    return bool(R18_RE.match(_plain(event)))


# nonebot Rule 按参数名识别依赖（event 为魔法名），不能用任意命名的 lambda
def _search_rule(event) -> bool:
    return _is_search(event) and _access_ok(event)


def _r18_rule(event) -> bool:
    return _is_r18(event) and _access_ok(event)


matcher = on_message(Rule(_search_rule), priority=10, block=True)
r18_matcher = on_message(Rule(_r18_rule), priority=10, block=True)


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
    """下载 QQ 消息里的图片；pximg CDN 偶发 5xx，带退避重试。"""
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://booth.pm/"}
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                if len(resp.content) < 100:
                    raise booth_client.BoothCliError("图片下载内容异常（过短）")
                return resp.content
        except httpx.HTTPStatusError as e:
            last_err = e
            if e.response.status_code < 500:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))
    raise last_err


def _ai_backend() -> tuple[str, str]:
    """返回 (mode, resolved_param)。cli 模式返回 opencode 可执行文件路径；
    api 模式返回第二个元素无意义。AI 整体不可用时返回 ("", "")。"""
    cfg = plugin_config
    if cfg.ai_mode == "cli":
        try:
            return "cli", vision.resolve_cli_bin(cfg.ai_cli_bin)
        except RuntimeError as e:
            logger.warning(f"opencode CLI 不可用: {e}")
            return "", ""
    if cfg.vision_api_key:
        return "api", ""
    return "", ""


async def _ai_translate(text: str) -> list:
    cfg = plugin_config
    mode, param = _ai_backend()
    if mode == "cli":
        return await vision.translate_keywords_cli(
            text, bin_path=param, model=cfg.ai_cli_model,
            timeout=cfg.ai_cli_timeout)
    return await vision.translate_keywords(
        text, base_url=cfg.vision_base_url, api_key=cfg.vision_api_key,
        model=cfg.vision_model, session_id=cfg.vision_session_id,
        timeout=cfg.vision_timeout)


async def _ai_vision(image_path: str, hint: str) -> tuple[list, str]:
    cfg = plugin_config
    mode, param = _ai_backend()
    if mode == "cli":
        return await vision.extract_keywords_cli(
            image_path, hint=hint, bin_path=param,
            model=cfg.ai_cli_model, timeout=cfg.ai_cli_timeout)
    image_bytes = Path(image_path).read_bytes()
    return await vision.extract_keywords(
        image_bytes, hint=hint, base_url=cfg.vision_base_url,
        api_key=cfg.vision_api_key, model=cfg.vision_model,
        session_id=cfg.vision_session_id, timeout=cfg.vision_timeout)


async def _handle_image(image_url: str, hint: str) -> str:
    cfg = plugin_config
    try:
        image_bytes = await _download_image(image_url)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        tip = ("图床临时不可用（5xx），请稍后重发"
               if code >= 500 else f"图片下载失败（HTTP {code}），确认图片链接有效")
        return f"⚠ {tip}"
    except httpx.TransportError:
        return "⚠ 图片下载失败：网络异常，请稍后重发"

    # 1) 识图 AI 提词（AI 不可用则跳过，仅靠 CLI 自带派生词）
    keywords, item_type = [], ""
    ai_note = ""
    mode, _ = _ai_backend()
    if mode:
        tmp_for_ai = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tf:
                tf.write(image_bytes)
                tmp_for_ai = tf.name
            keywords, item_type = await _ai_vision(tmp_for_ai, hint)
            logger.info(f"识图关键词: {keywords} (type={item_type})")
        except Exception as e:
            reason = vision.friendly_ai_error(e)
            logger.warning(f"识图 AI 失败（退化为纯图搜）: {e}")
            ai_note = f"⚠ AI 提词不可用：{reason}（已用图搜派生词）"
        finally:
            if tmp_for_ai:
                try:
                    os.unlink(tmp_for_ai)
                except OSError:
                    pass

    # 2) CLI 反向图搜
    matches = []
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tf:
            tf.write(image_bytes)
            tmp_path = tf.name
        data = booth_client.imgsearch(
            tmp_path, headless=cfg.imgsearch_headless,
            limit=cfg.booth_limit, cli_path=cfg.booth_cli_path,
            timeout=cfg.imgsearch_timeout)
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
        msg = ("没找到相关 Booth 商品。识别关键词: "
               + (" / ".join(keywords[:5]) or "（无）"))
        return f"{msg}\n{ai_note}" if ai_note else msg
    title = "识图关键词: " + (" / ".join(keywords[:5]) or "（无）") if keywords else "图搜结果:"
    out = format_results(merged, max_n=cfg.booth_limit, title=title)
    return f"{out}\n{ai_note}" if ai_note else out


def _handle_text_sync(hint: str, adult: str | None = None,
                      query_label: str | None = None) -> str:
    cfg = plugin_config
    res = booth_client.search(hint, limit=cfg.booth_limit, sort=cfg.booth_sort,
                              adult=adult or cfg.r18_mode,
                              cli_path=cfg.booth_cli_path,
                              timeout=cfg.search_timeout)
    items = res.get("items") or []
    if not items:
        return f"Booth 上没搜到「{hint}」（共 {res.get('total') or 0} 件）"
    total = f"共 {res['total']:,} 件，显示前 {len(items)}:" if res.get("total") else f"前 {len(items)}:"
    if query_label:
        total = f"{total}\nAI 关键词: {query_label}（原词「{hint}」）"
    for it in items:
        it.setdefault("via", "")
    return format_results(items, max_n=cfg.booth_limit, title=total)


async def _handle_text(hint: str, adult: str | None = None) -> str:
    """文本搜索入口：中文需求先经 AI 翻译成日语关键词；直搜空结果也用 AI 重试。
    AI 失败时给用户准确原因，并注明已退化为原词直搜。"""
    cfg = plugin_config
    ai_mode, _ = _ai_backend()
    ai_ready = bool(ai_mode)
    ai_note = ""  # AI 不可用时的用户反馈行

    if ai_ready and vision._looks_chinese(hint):
        try:
            kws = await _ai_translate(hint)
        except Exception as e:
            reason = vision.friendly_ai_error(e)
            logger.warning(f"中文关键词 AI 翻译失败（用原词直搜）: {e}")
            kws = []
            ai_note = f"⚠ AI 翻译不可用：{reason}（已用原词直搜）"
        if kws:
            try:
                res = booth_client.search(kws[0], limit=cfg.booth_limit,
                                          sort=cfg.booth_sort,
                                          adult=adult or cfg.r18_mode,
                                          cli_path=cfg.booth_cli_path,
                                          timeout=cfg.search_timeout)
                items = res.get("items") or []
            except booth_client.BoothCliError as e:
                logger.warning(f"AI 关键词搜索失败({kws[0]}): {e}")
                items = []
            if items:
                seen = {it["id"] for it in items}
                for kw in kws[1:2]:
                    try:
                        res2 = booth_client.search(kw, limit=cfg.booth_limit,
                                                   sort=cfg.booth_sort,
                                                   adult=adult or cfg.r18_mode,
                                                   cli_path=cfg.booth_cli_path,
                                                   timeout=cfg.search_timeout)
                    except booth_client.BoothCliError:
                        continue
                    for it in res2.get("items") or []:
                        if it["id"] not in seen:
                            it["via"] = "关键词"
                            seen.add(it["id"])
                            items.append(it)
                for it in items:
                    it.setdefault("via", "")
                total = (f"共 {res['total']:,} 件，显示前 {len(items)}:"
                         if res.get("total") else f"前 {len(items)}:")
                return format_results(items, max_n=cfg.booth_limit,
                                      title=f"{total}\nAI 关键词: {' / '.join(kws[:3])}（原词「{hint}」）")

    # 原词直搜；空结果且 AI 可用时翻译重试
    # （中文词在 Booth 常返回空搜索页，CLI 会抛 BoothCliError，按空结果处理）
    try:
        res = booth_client.search(hint, limit=cfg.booth_limit, sort=cfg.booth_sort,
                                  adult=adult or cfg.r18_mode,
                                  cli_path=cfg.booth_cli_path,
                                  timeout=cfg.search_timeout)
    except booth_client.BoothCliError as e:
        logger.warning(f"原词直搜无结果({hint}): {e}")
        res = {"items": [], "total": 0}
    if (res.get("items") or []) or not ai_ready:
        items = res.get("items") or []
        if not items:
            msg = f"Booth 上没搜到「{hint}」（共 {res.get('total') or 0} 件）"
            return f"{msg}\n{ai_note}" if ai_note else msg
        total = (f"共 {res['total']:,} 件，显示前 {len(items)}:"
                 if res.get("total") else f"前 {len(items)}:")
        for it in items:
            it.setdefault("via", "")
        out = format_results(items, max_n=cfg.booth_limit, title=total)
        return f"{out}\n{ai_note}" if ai_note else out

    try:
        kws = await _ai_translate(hint)
    except Exception as e:
        reason = vision.friendly_ai_error(e)
        logger.warning(f"AI 关键词翻译失败: {e}")
        msg = f"Booth 上没搜到「{hint}」（共 {res.get('total') or 0} 件）"
        return f"{msg}\n⚠ AI 翻译不可用：{reason}"
    if not kws:
        return f"Booth 上没搜到「{hint}」（共 {res.get('total') or 0} 件）"
    return _handle_text_sync(kws[0], adult=adult, query_label=" / ".join(kws[:3]))


@matcher.handle()
async def handle_vrc_search(bot, event: MessageEvent):
    m = SEARCH_RE.match(_plain(event))
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
            text = await _handle_text(hint)
    except booth_client.BoothCliError as e:
        text = f"搜索失败: {e}"
    except Exception as e:
        logger.exception("vrc search 未捕获异常")
        text = f"内部错误: {type(e).__name__}: {e}"
    await matcher.finish(text)


@r18_matcher.handle()
async def handle_vrc_r18(bot, event: MessageEvent):
    if not access.sensitive_allowed(plugin_config, getattr(event, "user_id", "")):
        await r18_matcher.finish(DENY_MSG)
    m = R18_RE.match(_plain(event))
    hint = (m.group(1) or "").strip() if m else ""
    if not hint:
        await r18_matcher.finish("用法: /vrc r18 <关键词>（R-18 专项搜索，仅管理员）")
    try:
        text = await _handle_text(hint, adult="only")
    except booth_client.BoothCliError as e:
        text = f"搜索失败: {e}"
    except Exception as e:
        logger.exception("vrc r18 未捕获异常")
        text = f"内部错误: {type(e).__name__}: {e}"
    await r18_matcher.finish(text)
