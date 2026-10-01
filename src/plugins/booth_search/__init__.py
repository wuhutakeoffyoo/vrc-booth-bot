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

from . import access, booth_client, forward, qcache, rank, vision, webfind
from .config import Config
from .image_download import ImageDownloadError, download_image
from .result_policy import filter_entries
from .format import format_results

plugin_config = get_plugin_config(Config)
# 全局并发闸：同时处理的查询上限（跨用户共享，GLOBAL_CONCURRENCY 可配）
_global_gate = access.ConcurrencyGate(plugin_config.global_concurrency)


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
    for attempt in range(3):
        try:
            return await download_image(url, allowed_hosts=plugin_config.image_allowed_hosts)
        except ImageDownloadError as e:
            if "HTTP 5" not in str(e) or attempt == 2:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))


def _ai_backend() -> tuple[str, str]:
    """返回 (mode, resolved_param)。cli 模式返回 opencode 可执行文件路径；
    api 模式返回第二个元素无意义。AI 整体不可用时返回 ("", "")。"""
    cfg = plugin_config
    if cfg.ai_mode == "cli":
        try:
            return "cli", vision.resolve_cli_bin(cfg.ai_cli_bin)
        except RuntimeError as e:
            logger.warning(f"opencode CLI 不可用: {e}")
    if cfg.vision_api_key or cfg.fallback_api_key:
        return "api", ""
    if cfg.ai_mode != "cli":
        try:
            return "cli", vision.resolve_cli_bin(cfg.ai_cli_bin)
        except RuntimeError:
            pass
    return "", ""


async def _ai_translate(text: str) -> tuple[list, list]:
    """【旧链路·归档未启用】中文需求 → 搜索方案 (标题关键词, 描述核实关键词)。
    文本链路已改 _ai_plan（智能体化方案，翻译由模型自决）；本包装仅存档，
    主 api（Go 套餐）→ 兜底 api（GLM Coding Plan）→ cli（mimo free）逐级回落。"""
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


async def _ai_recall(desc: str) -> list:
    """知名商品回忆：利用模型 VRChat 圈知识产出具体商品名（api 双路，不回落 cli）。"""
    cfg = plugin_config
    try:
        if not cfg.vision_api_key:
            raise BoothUnavailable("主 API 未配置")
        return await vision.recall_products(
            desc, base_url=cfg.vision_base_url, api_key=cfg.vision_api_key,
            model=cfg.vision_model, session_id=cfg.vision_session_id,
            timeout=cfg.vision_timeout)
    except Exception as e:
        logger.warning(f"主 api 回忆失败，尝试 GLM Coding Plan: {vision.friendly_ai_error(e)}")
    if cfg.fallback_api_key:
        try:
            return await vision.recall_products(
                desc, base_url=cfg.fallback_base_url, api_key=cfg.fallback_api_key,
                model=cfg.fallback_model, timeout=cfg.vision_timeout)
        except Exception as e:
            logger.warning(f"GLM Coding Plan 回忆失败: {vision.friendly_ai_error(e)}")
    return []


async def _ai_vision(image_path: str, hint: str) -> tuple[list, str]:
    cfg = plugin_config
    mode, param = _ai_backend()
    if mode == "api":
        try:
            if not cfg.vision_api_key:
                raise BoothUnavailable("主 API 未配置")
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


async def _enrich_entries(entries: list, desc_len: int = 600) -> None:
    """为候选补全商品详情（收藏数/真实 tags/上架日期/简介 _desc 供描述核实）。
    并发受限 3 路，单条失败静默跳过。"""
    cfg = plugin_config
    sem = asyncio.Semaphore(3)

    async def one(it: dict):
        async with sem:
            try:
                detail = await asyncio.to_thread(
                    booth_client.item, it.get("id"), desc_len=desc_len,
                    cli_path=cfg.booth_cli_path, timeout=cfg.search_timeout)
            except Exception:
                it["detail_status"] = "unavailable"
                return
            for k in ("wish_lists_count", "tags", "published_at", "is_adult", "is_vrchat"):
                v = detail.get(k)
                if v is not None:
                    it[k] = v
            it["_desc"] = detail.get("description") or ""
            it["detail_status"] = "available"

    await asyncio.gather(*[one(it) for it in entries])


async def _webfind_entries(hint: str, kws: list, adult: str | None = None) -> list:
    """网络检索兜底：站内搜不到时从 DDG/Exa 找 booth.pm 商品链接并抓详情。"""
    cfg = plugin_config
    if not cfg.websearch_fallback:
        return []
    q = (kws[0] if kws else hint)
    try:
        ids = await webfind.find_booth_item_ids(
            f"{q} Booth", exa_api_key=cfg.exa_api_key, timeout=15)
    except Exception as e:
        logger.warning(f"网络检索失败: {e}")
        return []
    entries = []
    for iid in ids[:3]:
        try:
            it = await asyncio.to_thread(
                booth_client.item, iid, cli_path=cfg.booth_cli_path,
                timeout=cfg.search_timeout)
            it["via"] = "网络检索"
            entries.append(it)
        except booth_client.BoothCliError:
            continue
    return filter_entries(entries, adult or cfg.r18_mode, cfg.vrc_tag)


async def _handle_image(image_url: str, hint: str) -> dict:
    try:
        image_bytes = await _download_image(image_url)
    except ImageDownloadError as e:
        return {"text": f"⚠ {e}", "entries": []}
    except httpx.HTTPError:
        return {"text": "⚠ 图片下载失败：网络异常，请稍后重发", "entries": []}
    return await _handle_image_bytes(image_bytes, hint)


async def _handle_image_bytes(image_bytes: bytes, hint: str) -> dict:
    """内部盲测入口：直接注入样本字节，不暴露消息 URL 本地读文件通道。"""
    cfg = plugin_config

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
    image_search_succeeded = False
    derived = ""  # Bing 对图内日文标题的 OCR，常含正确词形
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tf:
            tf.write(image_bytes)
            tmp_path = tf.name
        data = await asyncio.to_thread(
            booth_client.imgsearch, tmp_path, headless=cfg.imgsearch_headless,
            limit=max(cfg.booth_limit, 10), cli_path=cfg.booth_cli_path,
            timeout=cfg.imgsearch_timeout)
        matches = data.get("matches") or []
        image_search_succeeded = True
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
    # 视觉空转/无文字图：回忆兜底（模型 VRC 知识 + 派生词做描述）
    if not keywords and cfg.recall_enabled:
        desc = hint or derived or "VRChat 素材"
        try:
            keywords = [k for k in await _ai_recall(desc)][:2]
            logger.info(f"回忆兜底关键词: {keywords}")
        except Exception as e:
            logger.warning(f"回忆兜底失败: {e}")

    # 3) 关键词搜索合并：图搜派生词提到搜索队列最前；视觉关键词随后的前 3 个也搜；
    #    召回 limit 提到 10
    search_kws = list(dict.fromkeys(
        ([hint] if hint else []) + ([derived] if derived else []) + keywords))
    low_all = [k.lower() for k in search_kws if k]
    seen = {m.get("id") for m in matches}
    kw_hits = []
    recall_limit = max(cfg.booth_limit, 10)
    keyword_search_succeeded = False
    for kw in search_kws[:3]:
        try:
            res = await asyncio.to_thread(booth_client.search, kw, limit=recall_limit,
                                      sort=cfg.booth_sort, adult=cfg.r18_mode,
                                      tag=(cfg.vrc_tag or None), cli_path=cfg.booth_cli_path,
                                      timeout=cfg.search_timeout)
        except booth_client.BoothCliError as e:
            logger.warning(f"关键词搜索失败({kw}): {e}")
            continue
        keyword_search_succeeded = True
        for it in res.get("items") or []:
            it["_search_tag"] = cfg.vrc_tag
            if it["id"] not in seen:
                it["via"] = "关键词"
                seen.add(it["id"])
                kw_hits.append(it)
    # 合并排序：图搜全量候选与关键词命中统一重排——标题含任一关键词（视觉词/
    # 派生词/回忆词）的候选置顶（稳定排序），其余按原位次
    def _rel(it):
        name = (it.get("name") or "").lower()
        return any(k in name for k in low_all)
    merged = filter_entries(list(matches) + kw_hits, cfg.r18_mode, cfg.vrc_tag)
    dedup, seen2 = [], set()
    for it in merged:
        if it.get("id") not in seen2:
            seen2.add(it.get("id"))
            dedup.append(it)
    if low_all:
        dedup.sort(key=lambda it: not _rel(it))

    if not dedup:
        if not image_search_succeeded and not keyword_search_succeeded:
            return {"text": "搜索服务暂不可用：图搜及关键词请求未能完成，请稍后重试", "entries": []}
        msg = ("没找到相关 Booth 商品。识别关键词: "
               + (" / ".join(search_kws[:5]) or "（无）"))
        text = f"{msg}\n{ai_note}" if ai_note else msg
        return {"text": text, "entries": []}
    await _enrich_entries(dedup[:cfg.booth_limit])
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
            "entries": items, "header": total,
            "page": page, "qhint": hint, "total": res.get("total")}


def _effective_sort(page: int) -> tuple[str, str]:
    """返回 (实际排序, 标注)。Booth 在 popularity 排序下忽略 page 参数（站点行为），
    翻页时自动改按新着排序以保证翻页有效。"""
    cfg = plugin_config
    if page > 1 and cfg.booth_sort == "popularity":
        return "new", "（翻页按新着排序）"
    return cfg.booth_sort, ""


async def _search_merged(kws: list, adult: str | None = None, page: int = 1) -> tuple[list, dict]:
    """分词搜索（3 路并发）：Booth AND 分词对多词短语脆弱，单词级检索词命中率最高；
    连写形（ショコラドレス）整词保留。标题含词的候选置顶。
    返回 (merged_items, first_res)。单个关键词失败跳过。"""
    cfg = plugin_config
    sort, sort_note = _effective_sort(page)
    # 单词搜索深度 15：高热度泛词（スカート 等）popularity 头部的标题常不含词
    # （Booth 搜索匹配描述），加深让标题命中的候选浮出，供裁剪与排序使用
    limit = max(cfg.booth_limit, 15)
    terms, seen_t = [], set()
    for kw in kws[:6]:
        for tok in re.split(r"[\s/、，,]+", str(kw)):
            tok = tok.strip()
            # CJK 单字是合法词（鈴/耳），仅丢弃单字节/单字母噪音
            if (len(tok) >= 2 or (len(tok) == 1 and ord(tok) > 0x2E80)) \
                    and tok not in seen_t:
                seen_t.add(tok)
                terms.append(tok)
        whole = str(kw).strip()
        if " " in whole and whole not in seen_t:
            seen_t.add(whole)
            terms.append(whole)
    merged, seen, first_res = [], set(), {}
    sem = asyncio.Semaphore(3)

    async def _one(kw):
        async with sem:
            try:
                return await asyncio.to_thread(
                    booth_client.search, kw, limit=limit, sort=sort,
                    adult=adult or cfg.r18_mode, page=page,
                    tag=(cfg.vrc_tag or None), cli_path=cfg.booth_cli_path,
                    timeout=cfg.search_timeout)
            except booth_client.BoothCliError as e:
                logger.warning(f"关键词搜索失败({kw}): {e}")
                return None

    results = await asyncio.gather(*[_one(kw) for kw in terms[:6]])
    succeeded = sum(res is not None for res in results)
    failures = len(results) - succeeded
    for res in results:
        if res is None:
            continue
        if not first_res:
            first_res = res
        for it in res.get("items") or []:
            it["_search_tag"] = cfg.vrc_tag
            if it["id"] not in seen:
                it["via"] = "关键词"
                seen.add(it["id"])
                merged.append(it)
    low_kws = [k.lower() for k in terms if k]
    if low_kws:
        # 命中更多检索词的优先；同分内按首个命中词序位（鈴 优先于 ベル 的子串噪音）
        def _rank(it):
            name = (it.get("name") or "").lower()
            hit_idx = [i for i, k in enumerate(low_kws) if k in name]
            return (-len(hit_idx), hit_idx[0] if hit_idx else len(low_kws))

        merged.sort(key=_rank)
    if sort_note:
        first_res = dict(first_res or {})
        first_res["sort_note"] = sort_note
    first_res = dict(first_res)
    first_res["_search_successes"] = succeeded
    first_res["_search_failures"] = failures
    first_res["_all_failed"] = bool(results) and succeeded == 0
    first_res["has_next"] = any(res.get("has_next") for res in results if res is not None)
    return filter_entries(merged, adult or cfg.r18_mode, cfg.vrc_tag), first_res


async def _merged_zh_result(hint: str, merged: list, res: dict, kws: list,
                            desc_kws: list, page: int, page_note: str,
                            ai_note: str = "",
                            extra_notes: list | None = None) -> dict:
    """AI 关键词搜索的统一出口：可选描述核实重排 → 补详情 → 组装返回。

    desc_kws 非空时（『适用于XX素体』类需求）拉大详情池、简介扩长，
    按商品说明文匹配置顶——兼容信息（対応素体/仕様）写在说明里而非标题，
    只搜标题永远碰不到；核实结果在 header 里注明，让用户知道降级与否。
    extra_notes（评估结论等）追加进 header 与 notes。
    """
    cfg = plugin_config
    extra_notes = extra_notes or []
    shown = min(len(merged), cfg.booth_limit)
    sn = res.get("sort_note") or ""
    header = (f"共 {res.get('total') or 0:,} 件，显示前 {shown}:{page_note}{sn}"
              if res.get("total") else f"前 {shown}:{page_note}{sn}")
    header = f"{header}\nAI 关键词: {' / '.join(kws[:3])}（原词「{hint}」）"
    if res.get("_search_failures"):
        header += "\n部分关键词请求失败，展示已取得的结果"
    for note in extra_notes:
        header += f"\n{note}"
    if not desc_kws and len(merged) > cfg.booth_limit:
        # 标题命中足够时丢弃不相关填充：多词合并会把其他词的 popularity 结果
        # 混进来，没有本裁剪时「铃铛」会带回鸟居/泳装这类完全无关的商品
        low = [k.lower() for k in kws if k]
        matched = [it for it in merged
                   if any(k in (it.get("name") or "").lower() for k in low)]
        if len(matched) >= cfg.booth_limit:
            merged = matched
    if desc_kws:
        await _enrich_entries(merged[:15], desc_len=2000)
        merged = rank.desc_boost(merged, kws, desc_kws)
        n_hit = sum(1 for it in merged if rank.description_status(it, desc_kws) == "mentioned")
        header += (f"\n商品说明中提及「{' / '.join(desc_kws[:2])}」:{n_hit} 件（未确认兼容性）"
                   if n_hit else
                   "\n商品说明未提供可确认的兼容性证据，按关键词相关度展示")
    else:
        await _enrich_entries(merged[:cfg.booth_limit])
    return {"text": format_results(merged, max_n=cfg.booth_limit, title=header),
            "entries": merged, "header": header,
            "notes": ([ai_note] if ai_note else []) + extra_notes,
            "page": page, "qhint": hint, "total": res.get("total"),
            "has_next": res.get("has_next")}


async def _ai_plan(text: str, feedback: str = "") -> tuple[list, list, bool]:
    """需求 → 搜索方案（主 api → 兜底 api）。都不可用抛 BoothUnavailable。
    feedback：保守重试场景告知第一轮教训。返回 (标题关键词, desc_keywords, 是否翻译)。"""
    cfg = plugin_config
    mode, param = _ai_backend()
    if mode == "cli":
        try:
            return await vision.plan_search_cli(
                text, bin_path=param, model=cfg.ai_cli_model,
                timeout=cfg.ai_cli_timeout, feedback=feedback)
        except Exception as e:
            logger.warning(f"CLI 方案规划失败: {vision.friendly_ai_error(e)}")
    if cfg.vision_api_key:
        try:
            return await vision.plan_search(
                text, base_url=cfg.vision_base_url, api_key=cfg.vision_api_key,
                model=cfg.vision_model, session_id=cfg.vision_session_id,
                timeout=cfg.vision_timeout, feedback=feedback)
        except Exception as e:
            logger.warning(f"主 api 方案规划失败: {vision.friendly_ai_error(e)}")
    if cfg.fallback_api_key:
        try:
            return await vision.plan_search(
                text, base_url=cfg.fallback_base_url, api_key=cfg.fallback_api_key,
                model=cfg.fallback_model, timeout=cfg.vision_timeout,
                feedback=feedback)
        except Exception as e:
            logger.warning(f"兜底 api 方案规划失败: {vision.friendly_ai_error(e)}")
    if mode != "cli":
        try:
            return await vision.plan_search_cli(
                text, bin_path=vision.resolve_cli_bin(cfg.ai_cli_bin),
                model=cfg.ai_cli_model, timeout=cfg.ai_cli_timeout, feedback=feedback)
        except Exception:
            pass
    raise BoothUnavailable("方案规划不可用（主/兜底 api/cli）")


async def _ai_evaluate(query: str, keywords: list, titles: list) -> dict:
    """结果评估（主 api → 兜底 api）。都不可用抛 BoothUnavailable。"""
    cfg = plugin_config
    mode, param = _ai_backend()
    if mode == "cli":
        try:
            return await vision.evaluate_results_cli(
                query, keywords, titles, bin_path=param, model=cfg.ai_cli_model,
                timeout=cfg.ai_cli_timeout)
        except Exception as e:
            logger.warning(f"CLI 结果评估失败: {vision.friendly_ai_error(e)}")
    if cfg.vision_api_key:
        try:
            return await vision.evaluate_results(
                query, keywords, titles, base_url=cfg.vision_base_url,
                api_key=cfg.vision_api_key, model=cfg.vision_model,
                session_id=cfg.vision_session_id, timeout=cfg.vision_timeout)
        except Exception as e:
            logger.warning(f"主 api 结果评估失败: {vision.friendly_ai_error(e)}")
    if cfg.fallback_api_key:
        try:
            return await vision.evaluate_results(
                query, keywords, titles, base_url=cfg.fallback_base_url,
                api_key=cfg.fallback_api_key, model=cfg.fallback_model,
                timeout=cfg.vision_timeout)
        except Exception as e:
            logger.warning(f"兜底 api 结果评估失败: {vision.friendly_ai_error(e)}")
    if mode != "cli":
        try:
            return await vision.evaluate_results_cli(
                query, keywords, titles, bin_path=vision.resolve_cli_bin(cfg.ai_cli_bin),
                model=cfg.ai_cli_model, timeout=cfg.ai_cli_timeout)
        except Exception:
            pass
    raise BoothUnavailable("结果评估不可用（主/兜底 api/cli）")


async def _handle_text(hint: str, adult: str | None = None, page: int = 1,
                       notify=None) -> dict:
    """文本搜索入口（智能体化链路）：
    阶段 1 搜索方案——LLM 理解需求并自行决定是否翻译（翻译只是可选项）；
    阶段 2 结果评估——LLM 对照需求评估候选，不满意给第二轮关键词，
    经 notify 通知「正在执行第二次搜索」后重搜；
    阶段 3 再评估——确认命中或如实说明。AI 不可用时退化为原词分词直搜。
    notify: 可选异步回调（QQ 侧传 matcher.send），用于过程通知。
    返回 {"text", "entries", "header", "notes"}。"""
    cfg = plugin_config
    page_note = f"（第 {page} 页）" if page > 1 else ""
    ai_mode, _ = _ai_backend()
    ai_ready = bool(ai_mode)
    ai_notes: list = []

    async def _notify(msg: str):
        if notify is None:
            return
        try:
            await notify(msg)
        except Exception as e:
            logger.warning(f"过程通知发送失败: {e}")

    # 阶段 1：搜索方案（翻译由模型自行决定，不再是固定步骤）
    kws: list = []
    desc_kws: list = []
    if ai_ready:
        try:
            kws, desc_kws, translated = await _ai_plan(hint)
            logger.info(f"搜索方案: {kws} (translated={translated}, desc={desc_kws})")
        except Exception as e:
            logger.warning(f"AI 方案规划失败（用原词检索）: {e}")
            ai_notes.append(f"⚠ AI 方案规划不可用：{vision.friendly_ai_error(e)}（已用原词检索）")
    if kws:
        # 行业同义词种子层：泛称直译漏掉的硬映射（墨镜→サングラス 等）
        kws = vision.apply_industry_synonyms(hint, kws)
        kws = vision.expand_reading_variants(kws)

    merged, res = await _search_merged(kws or [hint], adult, page)
    if not merged and kws:
        # 方案词无果，退回原词直搜（日文输入时方案词可能反而偏）
        merged, res2 = await _search_merged([hint], adult, page)
        if res2.get("_search_successes") or (res2.get("total") and not res.get("total")):
            res = res2

    # 标题裁剪（desc 空时）：标题命中足够即丢弃不相关填充，评估看的就是裁剪后的候选
    if not desc_kws and len(merged) > cfg.booth_limit:
        low = [k.lower() for k in (kws or [hint]) if k]
        matched = [it for it in merged
                   if any(k in (it.get("name") or "").lower() for k in low)]
        if len(matched) >= cfg.booth_limit:
            merged = matched

    # 阶段 2：结果评估——不满意则第二轮搜索；阶段 3 再评估
    eval_note = ""
    used_terms = kws or [hint]
    if merged and ai_ready:
        try:
            titles = [f"{i}. {(it.get('name') or '(标题未知)')[:44]}"
                      for i, it in enumerate(merged[:12], 1)]
            ev = await _ai_evaluate(hint, used_terms, titles)
            ev = vision.validate_evaluation(ev, titles)
            logger.info(f"第一轮评估: {ev.get('verdict')} {ev.get('reason')}")
            # 保守 retry：评估员放行但标题命中率过低时仍触发二轮
            need_retry = (ev.get("verdict") == "retry" and bool(ev.get("keywords"))) or \
                vision.conservative_retry(ev.get("verdict", ""),
                                          [it.get("name") or "" for it in merged[:6]],
                                          used_terms)
            if need_retry:
                await _notify("第一轮结果不太对，正在执行第二轮搜索…")
                if ev.get("keywords"):
                    kws2 = vision.expand_reading_variants(ev["keywords"])[:6]
                else:
                    # 评估没给词：带教训重新规划
                    try:
                        kws2, _, _ = await _ai_plan(
                            hint, feedback=f"关键词 {used_terms[:4]} 无效（{ev.get('reason') or '候选不相关'}）")
                        kws2 = vision.expand_reading_variants(kws2)[:6]
                    except Exception as e2:
                        logger.warning(f"二轮重新规划失败: {e2}")
                        kws2 = []
                if kws2:
                    merged2, res2 = await _search_merged(kws2, adult, page)
                    if merged2:
                        seen = {it["id"] for it in merged2}
                        merged = merged2 + [it for it in merged if it["id"] not in seen]
                        used_terms = kws2
                        if res2.get("total"):
                            res = res2
                        # 阶段 3：再评估
                        try:
                            titles2 = [f"{i}. {(it.get('name') or '(标题未知)')[:44]}"
                                       for i, it in enumerate(merged[:12], 1)]
                            ev2 = await _ai_evaluate(hint, used_terms, titles2)
                            ev2 = vision.validate_evaluation(ev2, titles2)
                            eval_note = ("第二轮结果已按需求确认" if ev2.get("verdict") == "ok"
                                         else "两轮搜索后仍未完全确认，以下为最接近的结果"
                                              "（可补充材质/颜色/用途等描述再试）")
                        except Exception as e2:
                            logger.warning(f"第二轮评估失败: {e2}")
                            eval_note = "已完成第二轮搜索"
                    else:
                        eval_note = "第二轮搜索无结果，以下保留第一轮结果"
        except Exception as e:
            logger.warning(f"结果评估失败（按第一轮返回）: {e}")

    if not merged:
        web_entries = await _webfind_entries(hint, used_terms, adult)
        if web_entries:
            return {"text": format_results(web_entries, max_n=3,
                    title=f"网络检索命中（站内无「{hint}」）:"), "entries": web_entries}
        if res.get("_all_failed"):
            return {"text": "搜索服务暂不可用：站内请求均失败，请稍后重试", "entries": []}
        msg = f"Booth 上没搜到「{hint}」{page_note}"
        if used_terms:
            msg += f"，AI 关键词（{' / '.join(used_terms[:3])}）也未命中"
        if ai_notes:
            msg += "\n" + "\n".join(ai_notes)
        return {"text": msg, "entries": []}

    header_notes = ([eval_note] if eval_note else []) + ai_notes
    return await _merged_zh_result(hint, merged, res, used_terms, desc_kws,
                                   page, page_note, "; ".join(ai_notes),
                                   extra_notes=([eval_note] if eval_note else []))


async def _send_result(bot, event, result: dict, from_cache: bool = False):
    """优先合并转发（含商品图），失败或无条目时回退纯文本（注明降级原因）。
    超时类失败由 forward.send 按「可能已送达」处理，不会走到这里造成重复。"""
    cfg = plugin_config
    adult = "only" if result.get("command") == "r18" else cfg.r18_mode
    entries = filter_entries(result.get("entries") or [], adult, cfg.vrc_tag)
    header = result.get("header") or ""
    if from_cache and header:
        header = header + "\n（缓存结果，可能非最新）"
    text = result["text"]
    if len(entries) != len(result.get("entries") or []):
        header = f"符合当前筛选条件的结果：{len(entries)} 件"
        text = format_results(entries, max_n=cfg.booth_limit, title=header)
    if cfg.forward_messages and entries:
        try:
            nodes = await forward.build_result_nodes(
                int(bot.self_id), header,
                result.get("notes") or [], entries, max_n=cfg.booth_limit,
                page=result.get("page", 1), query_hint=result.get("qhint", ""),
                total=result.get("total"), has_next=result.get("has_next"),
                command=result.get("command", "search"), allowed_hosts=cfg.image_allowed_hosts)
        except Exception as e:
            logger.warning(f"转发节点构建失败: {e}")
            nodes = None
        if nodes:
            if await forward.send(bot, event, nodes):
                return
            text = "⚠ 合并转发发送失败，改用文字版：\n" + text
    if from_cache and result.get("header"):
        text = "(缓存结果，可能非最新)\n" + text
    await matcher.finish(text)


@matcher.handle()
async def handle_vrc_search(bot, event: MessageEvent):
    ok, wait = access.check_rate(plugin_config, getattr(event, "user_id", ""))
    if not ok:
        await matcher.finish(f"查询太频繁：请 {wait} 秒后再试"
                             f"（限流：每人 {plugin_config.user_cooldown} 秒间隔、"
                             f"每分钟 {plugin_config.user_rate_limit} 次）")
    if not _global_gate.try_acquire():
        await matcher.finish(f"当前已有 {plugin_config.global_concurrency} 个查询在处理，"
                             "通道满员，请稍后再试")
    try:
        await _do_search(bot, event)
    finally:
        _global_gate.release()


async def _do_search(bot, event: MessageEvent):
    m = SEARCH_RE.match(_plain(event))
    hint = (m.group(1) or "").strip() if m else ""
    hint, page = _split_page(hint)
    urls = _collect_image_urls(event)

    if not urls and not hint:
        await matcher.finish(USAGE)

    # 查询缓存：相同查询（模式/页码/词）在 TTL 内直接回缓存结果，省 AI 额度
    cache_key = qcache.make_key("search", plugin_config.r18_mode, page,
                                hint, urls[0] if urls else "", qcache.configuration_key(plugin_config))
    cached = (qcache.get(cache_key, plugin_config.query_cache_ttl)
              if plugin_config.query_cache_ttl > 0 and not urls else None)
    if cached is not None:
        logger.info(f"查询缓存命中: {cache_key[:60]}")
        await _send_result(bot, event, cached, from_cache=True)
        return

    try:
        if urls:
            if not hint:
                await matcher.send("收到图片，正在反查 Booth（10-60 秒）…")
            else:
                await matcher.send("收到图片+提示，正在反查 Booth（10-60 秒）…")
            result = await _handle_image(urls[0], hint)
        else:
            await matcher.send("Booth 搜索中，请稍候（智能链路含 AI 规划与评估，约 30-120 秒）…")
            result = await _handle_text(hint, page=page, notify=matcher.send)
    except booth_client.BoothCliError as e:
        result = {"text": f"搜索失败: {e}", "entries": []}
    except Exception as e:
        logger.exception("vrc search 未捕获异常")
        result = {"text": f"内部错误: {type(e).__name__}: {e}", "entries": []}
    if result.get("entries") and plugin_config.query_cache_ttl > 0:
        qcache.put(cache_key, result, ttl=plugin_config.query_cache_ttl,
                   max_entries=plugin_config.query_cache_max)
    await _send_result(bot, event, result)


@r18_matcher.handle()
async def handle_vrc_r18(bot, event: MessageEvent):
    ok, wait = access.check_rate(plugin_config, getattr(event, "user_id", ""))
    if not ok:
        await r18_matcher.finish(f"查询太频繁：请 {wait} 秒后再试"
                                 f"（限流：每人 {plugin_config.user_cooldown} 秒间隔、"
                                 f"每分钟 {plugin_config.user_rate_limit} 次）")
    if not access.sensitive_allowed(plugin_config, getattr(event, "user_id", "")):
        await r18_matcher.finish(DENY_MSG)
    if not _global_gate.try_acquire():
        await r18_matcher.finish(f"当前已有 {plugin_config.global_concurrency} 个查询在处理，"
                                 "通道满员，请稍后再试")
    try:
        await _do_r18(bot, event)
    finally:
        _global_gate.release()


async def _do_r18(bot, event: MessageEvent):
    m = R18_RE.match(_plain(event))
    hint = (m.group(1) or "").strip() if m else ""
    hint, page = _split_page(hint)
    if not hint:
        await r18_matcher.finish("用法: /vrc r18 <关键词>（R-18 专项搜索，仅管理员）")
    cache_key = qcache.make_key("r18", page, hint, qcache.configuration_key(plugin_config))
    cached = (qcache.get(cache_key, plugin_config.query_cache_ttl)
              if plugin_config.query_cache_ttl > 0 else None)
    if cached is not None:
        await _send_result(bot, event, cached, from_cache=True)
        return
    try:
        result = await _handle_text(hint, adult="only", page=page,
                                    notify=r18_matcher.send)
        result["command"] = "r18"
    except booth_client.BoothCliError as e:
        result = {"text": f"搜索失败: {e}", "entries": []}
    except Exception as e:
        logger.exception("vrc r18 未捕获异常")
        result = {"text": f"内部错误: {type(e).__name__}: {e}", "entries": []}
    if result.get("entries") and plugin_config.query_cache_ttl > 0:
        qcache.put(cache_key, result, ttl=plugin_config.query_cache_ttl,
                   max_entries=plugin_config.query_cache_max)
    await _send_result(bot, event, result)
