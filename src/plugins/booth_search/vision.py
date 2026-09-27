"""识图 AI 客户端：OpenAI 兼容 chat/completions 多模态接口（GLM / DeepSeek 等均可）。

key 只从配置读取，本模块不落任何凭据。
"""
import base64
import json
import re

import httpx

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


async def extract_keywords(image_bytes: bytes, *, hint: str = "",
                           base_url: str, api_key: str, model: str,
                           timeout: int = 60) -> tuple[list, str]:
    """调用识图模型，返回 (keywords, item_type)。HTTP 错误抛 httpx.HTTPStatusError。"""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": build_messages(hint, image_bytes),
        "temperature": 0.2,
        "max_tokens": 500,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {api_key}"})
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    return parse_keywords(content)
