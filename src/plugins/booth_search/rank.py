"""结果相关度重排（纯函数，可单测）。

desc_hit/desc_boost 服务于『适用于XX素体』类需求：Booth 把対応素体/仕様
这类兼容信息写在商品说明文里而非标题，需要拉详情后按说明文匹配，
把核实命中的候选置顶。
"""
from typing import Any


def desc_hit(it: dict, desc_kws: list) -> bool:
    """商品标题或简介（商品说明）含任一核实词（大小写不敏感子串）。"""
    hay = ((it.get("name") or "") + "\n" + (it.get("_desc") or "")).lower()
    return any(str(k).lower() in hay for k in desc_kws if k)


def desc_boost(merged: list, title_kws: list, desc_kws: list) -> list:
    """三级稳定重排：说明文/标题含核实词 → 标题含搜索词 → 其余。"""
    tkws = [str(k).lower() for k in title_kws if k]

    def _tier(it: Any) -> int:
        if desc_kws and desc_hit(it, desc_kws):
            return 0
        name = (it.get("name") or "").lower()
        return 1 if any(k in name for k in tkws) else 2

    return sorted(merged, key=_tier)
