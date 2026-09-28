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

from . import access, booth_client, forward, vision
from .config import Config
from .format import format_results

plugin_config = get_plugin_config(Config)


class BoothUnavailable(RuntimeError):
    """所有 AI 后端均不可用。"""


SEARCH_RE = re.compile(r"^[/！!]?vrc\s*search(?:\s+(.*))?$", re.I | re.S)
R18_RE = re.compile(r"^[/！!]?vrc\s*r18(?:\s+(.*))?$", re.I | re.S)
PAGE_RE = re.compile(r"^(.*?\S)\s+(\d{1,3})$")
USAGE = ("用法:\n/vrc search <关键词>   —— Booth 商品搜索\n"
         "/vrc search <关键词> <页码> —— 翻页（如 /vrc search 猫娘女仆装 2）\n"
         "/vrc search + 图片     —— 以图搜图（可附文字提示）")
DENY_MSG = "该指令仅对管理员开放"


def _split_page(text: str) -> tuple[str, int]:
    """查询末尾的独立数字（1-3 位）作为页码剥出；无则第 1 页。"""
    m = PAGE_RE.match(text)
    if m and m.group(1).strip():
        return m.group(1).strip(), max(1, int(m.group(2)))
    return text, 1


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
    """主 api（Go 套餐）→ 兜底 api（GLM Coding Plan）→ cli（mimo free）逐级回落。"""
    cfg = plugin_config
    mode, param = _ai_backend()
    if mode == "api":
        try:
            return await vision.translate_keywords(
                text, base_url=cfg.vision_base_url, api_key=cfg.vision_api_key,
                model=cfg.vision_model, session_id=cfg.vision_session_id,
                timeout=cfg.vision_timeout)
        except Exception as e:
            logger.warning(f"主 api 失败，尝试 GLM Coding Plan 兜底: {vision.friendly_ai_error(e)}")
        if cfg.fallback_api_key:
            try:
                return await vision.translate_keywords(
                    text, base_url=cfg.fallback_base_url,
                    api_key=cfg.fallback_api_key, model=cfg.fallback_model,
                    timeout=cfg.vision_timeout)
            except Exception as e:
                logger.warning(f"GLM Coding Plan 兜底失败，尝试 cli: {vision.friendly_ai_error(e)}")
        try:
            cli_bin = vision.resolve_cli_bin(cfg.ai_cli_bin)
        except RuntimeError as e:
            raise BoothUnavailable(f"所有 AI 后端均不可用（主 api / 兜底 api / cli）: {e}")
        return await vision.translate_keywords_cli(
            text, bin_path=cli_bin, model=cfg.ai_cli_model,
            timeout=cfg.ai_cli_timeout)
    return await vision.translate_keywords_cli(
        text, bin_path=param, model=cfg.ai_cli_model,
        timeout=cfg.ai_cli_timeout)


async def _ai_vision(image_path: str, hint: str) -> tuple[list, str]:
    cfg = plugin_config
    mode, param = _ai_backend()
    if mode == "api":
        try:
            image_bytes = Path(image_path).read_bytes()
            result = await vision.extract_keywords(
                image_bytes, hint=hint, base_url=cfg.vision_base_url,
                api_key=cfg.vision_api_key, model=cfg.vision_model,
                session_id=cfg.vision_session_id, timeout=cfg.vision_timeout)
            if not result[0]:  # 空关键词（推理模型偶发空转）自动重试一次
                logger.warning("识图关键词为空，重试一次")
                result = await vision.extract_keywords(
                    image_bytes, hint=hint, base_url=cfg.vision_base_url,
                    api_key=cfg.vision_api_key, model=cfg.vision_model,
                    session_id=cfg.vision_session_id, timeout=cfg.vision_timeout)
            return result
        except Exception as e:
            logger.warning(f"主 api 失败，尝试 GLM Coding Plan 兜底: {vision.friendly_ai_error(e)}")
        if cfg.fallback_api_key:
            try:
                image_bytes = Path(image_path).read_bytes()
                result = await vision.extract_keywords(
                    image_bytes, hint=hint, base_url=cfg.fallback_base_url,
                    api_key=cfg.fallback_api_key, model=cfg.fallback_model,
                    timeout=cfg.vision_timeout)
                if not result[0]:
                    logger.warning("兜底识图关键词为空，重试一次")
                    result = await vision.extract_keywords(
                        image_bytes, hint=hint, base_url=cfg.fallback_base_url,
                        api_key=cfg.fallback_api_key, model=cfg.fallback_model,
                        timeout=cfg.vision_timeout)
                return result
            except Exception as e:
                logger.warning(f"GLM Coding Plan 兜底失败，尝试 cli: {vision.friendly_ai_error(e)}")
        try:
            cli_bin = vision.resolve_cli_bin(cfg.ai_cli_bin)
        except RuntimeError as e:
            raise BoothUnavailable(f"所有 AI 后端均不可用（主 api / 兜底 api / cli）: {e}")
        return await vision.extract_keywords_cli(
            image_path, hint=hint, bin_path=cli_bin,
            model=cfg.ai_cli_model, timeout=cfg.ai_cli_timeout)
    return await vision.extract_keywords_cli(
        image_path, hint=hint, bin_path=param,
        model=cfg.ai_cli_model, timeout=cfg.ai_cli_timeout)


async def _handle_image(image_url: str, hint: str) -> dict:
    cfg = plugin_config
    try:
        image_bytes = await _download_image(image_url)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        tip = ("图床临时不可用（5xx），请稍后重发"
               if code >= 500 else f"图片下载失败（HTTP {code}），确认图片链接有效")
        return {"text": f"⚠ {tip}", "entries": []}
    except httpx.TransportError:
        return {"text": "⚠ 图片下载失败：网络异常，请稍后重发", "entries": []}

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
    derived = ""  # Bing 对图内日文标题的 OCR，常含正确词形
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
        derived = (data.get("derived_query") or "").strip()
        if derived and derived not in keywords:
            keywords.append(derived)
    except booth_client.BoothCliError as e:
        logger.warning(f"图搜失败: {e}")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # 3) 关键词搜索合并：图搜派生词提到搜索队列最前；视觉关键词随后的前 3 个也搜；
    #    召回 limit 提到 10
    search_kws = list(dict.fromkeys(
        ([derived] if derived else []) + keywords))
    low_all = [k.lower() for k in search_kws if k]
    seen = {m.get("id") for m in matches}
    kw_hits = []
    recall_limit = max(cfg.booth_limit, 10)
    for kw in search_kws[:3]:
        try:
            res = booth_client.search(kw, limit=recall_limit,
                                      sort=cfg.booth_sort, adult=cfg.r18_mode,
                                      tag=(cfg.vrc_tag or None), cli_path=cfg.booth_cli_path,
                                      timeout=cfg.search_timeout)
        except booth_client.BoothCliError as e:
            logger.warning(f"关键词搜索失败({kw}): {e}")
            continue
        for it in res.get("items") or []:
            if it["id"] not in seen:
                it["via"] = "关键词"
                seen.add(it["id"])
                kw_hits.append(it)
    # 合并排序：图搜视觉候选 Top2 置前（最相关）→ 标题含关键词的命中 →
    # 其余关键词命中 → 其余图搜候选（CLI 验证过的交错序）
    def _rel(it):
        name = (it.get("name") or "").lower()
        return any(k in name for k in low_all)
    kw_sorted = sorted(kw_hits, key=lambda it: not _rel(it))
    merged = matches[:2] + kw_sorted + list(matches[2:])
    dedup, seen2 = [], set()
    for it in merged:
        if it.get("id") not in seen2:
            seen2.add(it.get("id"))
            dedup.append(it)

    if not dedup:
        msg = ("没找到相关 Booth 商品。识别关键词: "
               + (" / ".join(search_kws[:5]) or "（无）"))
        text = f"{msg}\n{ai_note}" if ai_note else msg
        return {"text": text, "entries": []}
    title = "识图关键词: " + (" / ".join(search_kws[:5]) or "（无）") if search_kws else "图搜结果:"
    text = format_results(dedup, max_n=cfg.booth_limit, title=title)
    if ai_note:
        text = f"{text}\n{ai_note}"
    return {"text": text, "entries": dedup, "header": title, "notes": [ai_note] if ai_note else []}


def _handle_text_sync(hint: str, adult: str | None = None,
                      query_label: str | None = None, page: int = 1) -> dict:
    cfg = plugin_config
    sort, sort_note = _effective_sort(page)
    res = booth_client.search(hint, limit=cfg.booth_limit, sort=sort,
                              adult=adult or cfg.r18_mode, page=page,
                              tag=(cfg.vrc_tag or None), cli_path=cfg.booth_cli_path,
                              timeout=cfg.search_timeout)
    items = res.get("items") or []
    if not items:
        text = (f"Booth 上没搜到「{hint}」第 {page} 页（共 {res.get('total') or 0} 件）"
                if page > 1 else
                f"Booth 上没搜到「{hint}」（共 {res.get('total') or 0} 件）")
        return {"text": text, "entries": []}
    total = f"共 {res['total']:,} 件，显示前 {len(items)}:" if res.get("total") else f"前 {len(items)}:"
    if page > 1:
        total = f"「{hint}」第 {page} 页（共 {res.get('total') or 0:,} 件）:{sort_note}"
    if query_label:
        total = f"{total}\nAI 关键词: {query_label}（原词「{hint}」）"
    for it in items:
        it.setdefault("via", "")
    return {"text": format_results(items, max_n=cfg.booth_limit, title=total),
            "entries": items, "header": total}


def _effective_sort(page: int) -> tuple[str, str]:
    """返回 (实际排序, 标注)。Booth 在 popularity 排序下忽略 page 参数（站点行为），
    翻页时自动改按新着排序以保证翻页有效。"""
    cfg = plugin_config
    if page > 1 and cfg.booth_sort == "popularity":
        return "new", "（翻页按新着排序）"
    return cfg.booth_sort, ""


def _search_merged(kws: list, adult: str | None = None, page: int = 1) -> tuple[list, dict]:
    """按顺序搜索前 5 个单词级关键词并合并去重（召回 limit 提到 10，展示层再截断）。
    标题含任一关键词的候选置顶（稳定排序，对抗 popularity 淹没）。
    返回 (merged_items, first_res)。单个关键词失败跳过。"""
    cfg = plugin_config
    sort, sort_note = _effective_sort(page)
    limit = max(cfg.booth_limit, 10)
    merged, seen, first_res = [], set(), {}
    for kw in kws[:5]:
        try:
            res = booth_client.search(kw, limit=limit, sort=sort,
                                      adult=adult or cfg.r18_mode, page=page,
                                      tag=(cfg.vrc_tag or None), cli_path=cfg.booth_cli_path,
                                      timeout=cfg.search_timeout)
        except booth_client.BoothCliError as e:
            logger.warning(f"关键词搜索失败({kw}): {e}")
            continue
        if not first_res:
            first_res = res
        for it in res.get("items") or []:
            if it["id"] not in seen:
                it["via"] = "关键词"
                seen.add(it["id"])
                merged.append(it)
    low_kws = [k.lower() for k in kws[:3] if k]
    if low_kws:
        merged.sort(key=lambda it: not any(
            k in (it.get("name") or "").lower() for k in low_kws))
    if sort_note:
        first_res = dict(first_res or {})
        first_res["sort_note"] = sort_note
    return merged, first_res


async def _handle_text(hint: str, adult: str | None = None, page: int = 1) -> dict:
    """文本搜索入口：中文需求先经 AI 翻译成日语关键词；直搜空结果也用 AI 重试。
    AI 失败时给用户准确原因，并注明已退化为原词直搜。page 为结果页码。
    返回 {"text", "entries", "header", "notes"}。"""
    cfg = plugin_config
    page_note = f"（第 {page} 页）" if page > 1 else ""
    ai_mode, _ = _ai_backend()
    ai_ready = bool(ai_mode)
    ai_note = ""  # AI 不可用时的用户反馈行

    if (ai_ready and vision._looks_chinese(hint)
            and not re.search(r"[A-Za-z]{4,}", hint)):
        # 含 4 字以上拉丁词（商品原名/罗马字）时不走翻译——百样本实测原词直搜
        # 命中率远高于 AI 翻译（Top1 5/7 vs 0），翻译仅作空结果兜底
        try:
            kws = await _ai_translate(hint)
            kws = vision.expand_reading_variants(kws)  # 汉字词追加假名读音变体
        except Exception as e:
            reason = vision.friendly_ai_error(e)
            logger.warning(f"中文关键词 AI 翻译失败（用原词直搜）: {e}")
            kws = []
            ai_note = f"⚠ AI 翻译不可用：{reason}（已用原词直搜）"
        if kws:
            merged, res = _search_merged(kws, adult, page)
            if merged:
                shown = min(len(merged), cfg.booth_limit)
                sn = res.get("sort_note") or ""
                header = (f"共 {res.get('total') or 0:,} 件，显示前 {shown}:{page_note}{sn}"
                          if res.get("total") else f"前 {shown}:{page_note}{sn}")
                header = f"{header}\nAI 关键词: {' / '.join(kws[:3])}（原词「{hint}」）"
                return {"text": format_results(merged, max_n=cfg.booth_limit, title=header),
                        "entries": merged, "header": header,
                        "notes": [ai_note] if ai_note else []}

    # 原词直搜；空结果且 AI 可用时翻译重试
    # （中文词在 Booth 常返回空搜索页，CLI 会抛 BoothCliError，按空结果处理）
    sort, sort_note = _effective_sort(page)
    try:
        res = booth_client.search(hint, limit=cfg.booth_limit, sort=sort,
                                  adult=adult or cfg.r18_mode, page=page,
                                  tag=(cfg.vrc_tag or None), cli_path=cfg.booth_cli_path,
                                  timeout=cfg.search_timeout)
    except booth_client.BoothCliError as e:
        logger.warning(f"原词直搜无结果({hint}): {e}")
        res = {"items": [], "total": 0}
    if (res.get("items") or []) or not ai_ready:
        items = res.get("items") or []
        if not items:
            msg = f"Booth 上没搜到「{hint}」{page_note}（共 {res.get('total') or 0} 件）"
            return {"text": f"{msg}\n{ai_note}" if ai_note else msg, "entries": []}
        total = (f"共 {res['total']:,} 件，显示前 {len(items)}:{page_note}{sort_note}"
                 if res.get("total") else f"前 {len(items)}:{page_note}{sort_note}")
        for it in items:
            it.setdefault("via", "")
        text = format_results(items, max_n=cfg.booth_limit, title=total)
        return {"text": f"{text}\n{ai_note}" if ai_note else text,
                "entries": items, "header": total,
                "notes": [ai_note] if ai_note else []}

    try:
        kws = await _ai_translate(hint)
        kws = vision.expand_reading_variants(kws)  # 汉字词追加假名读音变体
    except Exception as e:
        reason = vision.friendly_ai_error(e)
        logger.warning(f"AI 关键词翻译失败: {e}")
        msg = f"Booth 上没搜到「{hint}」{page_note}（共 {res.get('total') or 0} 件）"
        return {"text": f"{msg}\n⚠ AI 翻译不可用：{reason}", "entries": []}
    if not kws:
        return {"text": f"Booth 上没搜到「{hint}」{page_note}（共 {res.get('total') or 0} 件）",
                "entries": []}
    merged, res = _search_merged(kws, adult, page)
    if not merged:
        text = (f"Booth 上没搜到「{hint}」{page_note}，"
                f"AI 关键词（{' / '.join(kws[:3])}）也未命中")
        return {"text": text, "entries": []}
    shown = min(len(merged), cfg.booth_limit)
    sn = res.get("sort_note") or ""
    header = (f"共 {res.get('total') or 0:,} 件，显示前 {shown}:{page_note}{sn}"
              if res.get("total") else f"前 {shown}:{page_note}{sn}")
    header = f"{header}\nAI 关键词: {' / '.join(kws[:3])}（原词「{hint}」）"
    for it in merged:
        it.setdefault("via", "")
    return {"text": format_results(merged, max_n=cfg.booth_limit, title=header),
            "entries": merged, "header": header}


async def _send_result(bot, event, result: dict):
    """优先合并转发（含商品图），失败或无条目时回退纯文本。"""
    cfg = plugin_config
    entries = result.get("entries") or []
    if cfg.forward_messages and entries:
        try:
            nodes = await forward.build_result_nodes(
                int(bot.self_id), result.get("header") or "",
                result.get("notes") or [], entries, max_n=cfg.booth_limit)
        except Exception as e:
            logger.warning(f"转发节点构建失败: {e}")
            nodes = None
        if nodes and await forward.send(bot, event, nodes):
            return
    await matcher.finish(result["text"])


@matcher.handle()
async def handle_vrc_search(bot, event: MessageEvent):
    m = SEARCH_RE.match(_plain(event))
    hint = (m.group(1) or "").strip() if m else ""
    hint, page = _split_page(hint)
    urls = _collect_image_urls(event)

    if not urls and not hint:
        await matcher.finish(USAGE)

    try:
        if urls:
            if not hint:
                await matcher.send("收到图片，正在反查 Booth（10-60 秒）…")
            else:
                await matcher.send("收到图片+提示，正在反查 Booth（10-60 秒）…")
            result = await _handle_image(urls[0], hint)
        else:
            result = await _handle_text(hint, page=page)
    except booth_client.BoothCliError as e:
        result = {"text": f"搜索失败: {e}", "entries": []}
    except Exception as e:
        logger.exception("vrc search 未捕获异常")
        result = {"text": f"内部错误: {type(e).__name__}: {e}", "entries": []}
    await _send_result(bot, event, result)


@r18_matcher.handle()
async def handle_vrc_r18(bot, event: MessageEvent):
    if not access.sensitive_allowed(plugin_config, getattr(event, "user_id", "")):
        await r18_matcher.finish(DENY_MSG)
    m = R18_RE.match(_plain(event))
    hint = (m.group(1) or "").strip() if m else ""
    hint, page = _split_page(hint)
    if not hint:
        await r18_matcher.finish("用法: /vrc r18 <关键词>（R-18 专项搜索，仅管理员）")
    try:
        result = await _handle_text(hint, adult="only", page=page)
    except booth_client.BoothCliError as e:
        result = {"text": f"搜索失败: {e}", "entries": []}
    except Exception as e:
        logger.exception("vrc r18 未捕获异常")
        result = {"text": f"内部错误: {type(e).__name__}: {e}", "entries": []}
    await _send_result(bot, event, result)
