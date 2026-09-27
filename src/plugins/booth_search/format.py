"""结果格式化（纯函数，可单测）。"""
from typing import Any


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
        lines.append(f"   {url}")
        shop = (it.get("shop") or {}).get("name")
        if shop:
            cat = f" · {it['category']}" if it.get("category") else ""
            lines.append(f"   店铺: {shop}{cat}")
    return "\n".join(lines)
