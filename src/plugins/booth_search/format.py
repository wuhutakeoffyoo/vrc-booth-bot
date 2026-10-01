"""结果格式化（纯函数，可单测）。"""
from typing import Any
try:
    from .search_evidence import evidence_label
except ImportError:
    from search_evidence import evidence_label


def price_str(price: Any) -> str:
    if isinstance(price, int):
        return f"¥{price:,}"
    if price:
        return str(price)
    return "价格未知"


def format_results(entries: list, max_n: int = 6, title: str = "") -> str:
    """entries: booth item dict（含可选 via/imgsearch 标记），合并后的候选列表。"""
    lines = []
    if title:
        lines.append(title)
    for i, it in enumerate(entries[:max_n], 1):
        flags = []
        if it.get("via"):
            flags.append(it["via"])
        if it.get("is_adult"):
            flags.append("R-18")
        if it.get("is_sold_out"):
            flags.append("已售罄")
        flag_s = f"  [{'|'.join(flags)}]" if flags else ""
        url = it.get("url") or f"https://booth.pm/ja/items/{it.get('id')}"
        lines.append(f"{i}. {it.get('name') or '(标题未知)'}  {price_str(it.get('price'))}{flag_s}")
        wl = it.get("wish_lists_count")
        if isinstance(wl, int) and wl > 0:
            lines.append(f"   ♥ {wl:,} 收藏")
        pub = str(it.get("published_at") or "")[:10]
        if pub:
            lines.append(f"   上架 {pub}")
        tags = [t for t in (it.get("tags") or []) if t][:5]
        if tags:
            lines.append("   " + " ".join("#" + str(t) for t in tags))
        lines.append(f"   {url}")
        if evidence_label(it):
            lines.append("   " + evidence_label(it))
        shop = (it.get("shop") or {}).get("name")
        if shop:
            cat = f" · {it['category']}" if it.get("category") else ""
            lines.append(f"   店铺: {shop}{cat}")
    return "\n".join(lines)
