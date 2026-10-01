"""结果相关度重排（纯函数，可单测）。

desc_hit/desc_boost 服务于『适用于XX素体』类需求：Booth 把対応素体/仕様
这类兼容信息写在商品说明文里而非标题，需要拉详情后按说明文匹配，
把核实命中的候选置顶。
"""
from typing import Any
import re


def desc_hit(it: dict, desc_kws: list) -> bool:
    """关键词相关度，不冒充兼容性确认。"""
    return description_status(it, desc_kws) in ("mentioned", "title_only")


def description_status(it: dict, desc_kws: list) -> str:
    terms = [str(k).casefold() for k in desc_kws if k]
    desc = (it.get("_desc") or "").casefold()
    matching = [s for s in re.split(r"[。！？\n]", desc) if any(k in s for k in terms)]
    negative = r"対応していません|非対応|未対応|not\s+(?:compatible|supported)|不(?:兼容|支持)"
    if any(re.search(negative, s) for s in matching):
        return "unsupported"
    if matching and it.get("detail_status") != "unavailable":
        return "mentioned"
    if any(k in (it.get("name") or "").casefold() for k in terms):
        return "title_only"
    return "unknown"


def desc_boost(merged: list, title_kws: list, desc_kws: list) -> list:
    """三级稳定重排：说明文/标题含核实词 → 标题含搜索词 → 其余。"""
    tkws = [str(k).lower() for k in title_kws if k]

    def _tier(it: Any) -> int:
        if desc_kws and desc_hit(it, desc_kws):
            return 0
        name = (it.get("name") or "").lower()
        return 1 if any(k in name for k in tkws) else 2

    return sorted(merged, key=_tier)
