"""识图 AI 客户端：双后端。

- api: OpenAI 兼容 chat/completions（OpenCode Zen Go 端点 / GLM 直连等）。
  Go 端点要求 x-opencode-session 头标识客户端会话。
- cli: 本机 opencode CLI（`opencode run`，free 模型可用，支持 -f 附图）

key 只从配置读取，本模块不落任何凭据。
"""
import asyncio
import base64
import ipaddress
import json
import re
import shutil
import subprocess
import urllib.parse
import uuid
from pathlib import Path

import httpx

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def guard_api_base(base_url: str) -> str:
    """出站前校验 API base：仅 https 且主机非本机/私网/保留地址。"""
    parts = urllib.parse.urlsplit(base_url)
    host = parts.hostname or ""
    if parts.scheme != "https" or not host:
        raise RuntimeError(f"VISION_BASE_URL 必须是 https: {base_url}")
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith((".local", ".internal")):
        raise RuntimeError(f"VISION_BASE_URL 主机不被允许: {host}")
    try:
        if ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_reserved:
            raise RuntimeError(f"VISION_BASE_URL 指向私网/保留地址: {host}")
    except ValueError:
        pass  # 域名（非 IP 字面量），放行
    return base_url


def _session_header(session_id: str) -> str:
    return session_id or f"booth-bot-{uuid.uuid4().hex[:16]}"

# 提词目标：VRChat 素材（模型/衣装/髪型/配件/ギミック/テクスチャ/ツール）
_VISION_PROMPT = (
    "你是 Booth.pm（VRChat 素材市场）的搜品助手。看图识别图中商品，"
    "给出用于 Booth.pm 站内搜索的关键词。要求：\n"
    "1. 优先提取图内文字里的商品名/角色名（片假名、罗马字、英文原样）；\n"
    "2. 判断商品类型（3Dモデル/衣装/髪型/アクセサリ/ギミック/テクスチャ/ツール等），"
    "补 1-2 个类型名词词轴；\n"
    "3. 图内无商品名时，按外观特征（发色发型/服装/配色/风格）给搜索词；\n"
    "4. 只输出 JSON，格式："
    '{"keywords": ["词1", "词2"], "item_type": "类型", "found_text": "图内文字"}'
)


def build_data_url(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def build_messages(hint: str, image_bytes: bytes) -> list:
    """构造多模态消息体。hint 是用户随图输入的补充文字。"""
    content = [{"type": "text", "text": _VISION_PROMPT}]
    if hint:
        content[0]["text"] += f"\n用户补充提示：{hint}"
    content.append({"type": "image_url",
                    "image_url": {"url": build_data_url(image_bytes)}})
    return [{"role": "user", "content": content}]


def parse_keywords(content: str) -> tuple[list, str]:
    """从模型回复中解析关键词列表与商品类型，容错（JSON 围栏/坏 JSON/纯文本）。"""
    item_type = ""
    m = re.search(r"\{.*\}", content or "", re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            kws = data.get("keywords") or []
            item_type = str(data.get("item_type") or "")
            if isinstance(kws, list):
                return [str(k).strip() for k in kws if str(k).strip()], item_type
        except (json.JSONDecodeError, AttributeError):
            pass
    # 兜底：按行/分隔符拆
    parts = re.split(r"[\n,，、/|]+", content or "")
    return [p.strip(" -·*") for p in parts if len(p.strip(" -·*")) >= 2][:8], item_type


def _api_content(resp_json: dict) -> str:
    """取模型回复文本；推理模型可能把内容放在 reasoning_content 或超长截断。"""
    choice = (resp_json.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content") or ""
    if not content.strip():
        content = msg.get("reasoning_content") or ""
    return content


async def _api_post(url: str, payload: dict, api_key: str,
                    session_id: str = "", timeout: int = 60,
                    retries: int = 2) -> str:
    """POST chat/completions，传输类错误自动重试，返回回复文本。
    UA 用浏览器标识：Cloudflare WAF 会拦数据中心 IP + python 默认 UA 的大 body POST。"""
    headers = {"Authorization": f"Bearer {api_key}",
               "x-opencode-session": _session_header(session_id),
               "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/126.0.0.0 Safari/537.36")}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(retries + 1):
            try:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                return _api_content(resp.json())
            except httpx.TransportError:
                if attempt == retries:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))


async def extract_keywords(image_bytes: bytes, *, hint: str = "",
                           base_url: str, api_key: str, model: str,
                           session_id: str = "", timeout: int = 60) -> tuple[list, str]:
    """调用识图模型，返回 (keywords, item_type)。HTTP 错误抛 httpx.HTTPStatusError。"""
    guard_api_base(base_url)
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": build_messages(hint, image_bytes),
        "temperature": 0.2,
        "max_tokens": 2000,
    }
    content = await _api_post(url, payload, api_key, session_id, timeout)
    return parse_keywords(content)


# 中文→Booth 日语关键词的文本翻译提示词
_TRANSLATE_PROMPT = (
    "用户在 Booth.pm（日本同人/VRChat 素材市场）找商品，但输入的是中文。"
    "把需求翻译成 2-4 个 Booth 站内最可能命中的日语搜索关键词"
    "（商品类型名词用日语行业叫法，如 3Dモデル/衣装/髪型/アクセサリ/ギミック/テクスチャ；"
    "专有名词保留罗马字/英文/片假名原样）。只输出 JSON："
    '{"keywords": ["日语词1", "日语词2"]}'
)


def _looks_chinese(text: str) -> bool:
    """检测简化字特有码位（日文汉字不在此列），用于中文需求识别。"""
    simplified_marks = "们这说图搜贴图价买卖买现视频动发经过关软件猫丝袜女仆装饰角色宠头像见听记应东车马语读谁错银钱购"
    return any(ch in simplified_marks for ch in text)


async def translate_keywords(text: str, *, base_url: str, api_key: str,
                             model: str, session_id: str = "",
                             timeout: int = 60) -> list:
    """中文需求 → 日语搜索关键词列表。HTTP/解析失败抛异常，由调用方退化。"""
    guard_api_base(base_url)
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": f"{_TRANSLATE_PROMPT}\n用户需求：{text}"}],
        "temperature": 0.2,
        "max_tokens": 2000,
    }
    content = await _api_post(url, payload, api_key, session_id, timeout)
    kws, _ = parse_keywords(content)
    return kws


# ---------------------------------------------------------------- cli 后端

def resolve_cli_bin(configured: str = "") -> str:
    """定位 opencode 可执行文件；找不到抛 RuntimeError。"""
    if configured:
        if shutil.which(configured) or Path(configured).is_file():
            return configured
        raise RuntimeError(f"AI_CLI_BIN 指向的 opencode 不存在: {configured}")
    found = shutil.which("opencode")
    if found:
        return found
    raise RuntimeError("找不到 opencode CLI（PATH 与 AI_CLI_BIN 均未命中）")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _cli_run_sync(bin_path: str, args: list, timeout: int) -> str:
    """阻塞执行 opencode run，返回清理 ANSI 后的 stdout；失败抛 RuntimeError。

    注意参数顺序：message 必须在 -f 之前（yargs 会把后续位置参数吞进 -f）。
    """
    try:
        proc = subprocess.run(
            [bin_path, "run"] + args,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"opencode run 超时（>{timeout}s）")
    if proc.returncode != 0:
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-200:]
        raise RuntimeError(f"opencode run 失败(exit {proc.returncode}): {tail}")
    return _strip_ansi(proc.stdout)


async def translate_keywords_cli(text: str, *, bin_path: str, model: str,
                                 timeout: int = 90) -> list:
    """CLI 后端：中文需求 → 日语关键词。输出必须含 JSON，否则视为失败。"""
    prompt = f"{_TRANSLATE_PROMPT}\n用户需求：{text}"
    out = await asyncio.to_thread(
        _cli_run_sync, bin_path, ["-m", model, prompt], timeout)
    if not re.search(r"\{.*\}", out, re.S):
        raise RuntimeError(f"opencode 翻译输出不含 JSON: {out[-160:]}")
    kws, _ = parse_keywords(out)
    if not kws:
        raise RuntimeError(f"opencode 翻译输出无法解析: {out[-160:]}")
    return kws


async def extract_keywords_cli(image_path: str, *, hint: str = "",
                               bin_path: str, model: str,
                               timeout: int = 90) -> tuple[list, str]:
    """CLI 后端：识图提词（-f 附带图片文件）。"""
    prompt = _VISION_PROMPT
    if hint:
        prompt += f"\n用户补充提示：{hint}"
    out = await asyncio.to_thread(
        _cli_run_sync, bin_path, ["-m", model, prompt, "-f", image_path], timeout)
    return parse_keywords(out)
