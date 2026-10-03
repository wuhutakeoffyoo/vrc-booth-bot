"""Small deterministic evidence/terminology helpers; no framework or AI calls."""
import hashlib
import json
import re
import unicodedata

NEGATIVE = re.compile(r"対応して(?:いません|おりません)|非対応|未対応|not\s+(?:compatible|supported)|不(?:兼容|支持)", re.I)
NEGATED_REQUEST = re.compile(r"不要|不想|不需要|不找|排除|别(?:给|要|找)?|除外|不要な|いらない|\b(?:without|exclude|not)\b", re.I)
GENERIC_TERMS = {"vrchat", "vrc", "booth", "3d", "3dモデル", "3d model", "アバター",
                 "avatar", "アクセサリ", "アクセサリー", "小物", "雑貨", "対応",
                 "衣装", "髪型", "ギミック", "テクスチャ", "素材", "asset", "assets"}


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def generic_term(value):
    key = normalized(value)
    parts = re.split(r"[\s/、，,]+", key)
    return key in GENERIC_TERMS or bool(parts) and all(part in GENERIC_TERMS for part in parts)


def literal_query(query, seeds=()):
    """Keep short target phrases, but never turn negated prose into a positive seed."""
    query = str(query or "").strip()
    if not query or len(query) > 48 or len(query.split()) > 5:
        return ""
    if re.search(r"[，,。；;！？!?\n]", query) or NEGATED_REQUEST.search(query):
        return ""
    if re.search(r"想(?:要|找|搜)|搜索|帮我|适用|兼容|対応|compatible|looking\s+for", query, re.I):
        return ""
    # Known Chinese concepts already have tested Japanese retrieval seeds.
    if positive_seeds(query, seeds):
        return ""
    return query


def retrieval_terms(query, proposed, seeds=(), *, previous=(), limit=6):
    """Reserve one of the existing slots for the original target; drop generic drift.

    Retry uses only new terms. The original axis was already fetched in round one,
    so it remains in the merged pool without spending another request.
    """
    anchor = literal_query(query, seeds)
    old = {normalized(term) for term in previous}
    seen, result = set(old), []
    for term in ([anchor] if anchor and not previous else []) + list(proposed or []):
        term = str(term or "").strip()
        key = normalized(term)
        if not key or key in seen:
            continue
        if generic_term(key) and key != normalized(query):
            continue
        seen.add(key)
        result.append(term)
    return result[:limit]


def literal_match(item, query):
    target = normalized(query)
    name = normalized(item.get("name"))
    if not target or not name:
        return False
    # ASCII word boundaries avoid Bell -> Bella while retaining CJK compounds.
    if re.fullmatch(r"[a-z0-9][a-z0-9 _-]*", target):
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(target) + r"(?![a-z0-9])", name))
    return target in name


def rank_target(entries, query, seeds=(), required_terms=()):
    anchor = literal_query(query, seeds)

    def key(item):
        state = item.get("relevance_status")
        if state == "unsupported":
            return 3
        if state == "related":
            return 0
        if anchor and not required_terms and literal_match(item, anchor):
            return 1
        return 2

    return sorted(entries, key=key)


def merge_rounds(first, second, query, seeds=(), required_terms=()):
    """Retain fetched details and first-round evidence when duplicate IDs recur."""
    by_id = {str(item["id"]): item for item in first}
    merged, seen = [], set()
    for item in list(second) + list(first):
        identity = str(item["id"])
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(by_id.get(identity, item))
    return rank_target(merged, query, seeds, required_terms)


def select_results(entries, query, seeds=(), required_terms=(), *, assessed=False, display_limit=None):
    """Return grounded hits and literal candidates without padding with unknowns.

    An outage must not erase all search candidates. Without positive evidence or
    literal matches, keep up to three explicitly unverified suggestions after an
    assessment; direct searches retain their normal result pool.
    """
    ordered = rank_target(entries, query, seeds, required_terms)
    eligible = [it for it in ordered if it.get("relevance_status") != "unsupported"]
    anchor = literal_query(query, seeds)
    selected = [it for it in eligible if it.get("relevance_status") == "related" or
                (anchor and not required_terms and literal_match(it, anchor))]
    if not selected:
        selected = eligible[:3] if assessed else eligible
    omitted = len(entries) - len(selected)
    if display_limit is not None:
        selected = selected[:display_limit]
    related = sum(it.get("relevance_status") == "related" for it in selected)
    quality = {"related": related, "unverified": len(selected) - related,
               "omitted": omitted}
    return selected, quality


def positive_seeds(query, seeds):
    query = unicodedata.normalize("NFKC", str(query or "")).casefold()
    clauses = re.split(r"[，,。;；\n]|但(?:是)?|而是|但是", query)
    found = []
    for seed in seeds:
        for clause in clauses:
            at = clause.find(seed.casefold())
            if at < 0:
                continue
            prefix = clause[:at]
            suffix = clause[at + len(seed):]
            if NEGATED_REQUEST.search(prefix) or re.match(r"\s*(?:不要|不需要|不要な|いらない)", suffix):
                continue
            found.append(seed)
            break
    return found


def excerpt(description, terms=(), max_chars=900):
    text = str(description or "")
    windows = []
    for term in terms:
        for match in list(re.finditer(re.escape(str(term)), text, re.I))[:2]:
            windows.append((max(0, match.start()-100), min(len(text), match.end()+180)))
    if not windows:
        windows = [(0, min(len(text), 600))]
        if len(text) > 600:
            windows.append((max(600, len(text)-250), len(text)))
    pieces = []
    previous_end = -1
    for start, end in sorted(windows):
        start = max(start, previous_end)
        if start < end:
            pieces.append(text[start:end])
            previous_end = end
    return "\n…\n".join(pieces)[:max_chars]


def source_fields(item):
    tags = [t.get("name", "") if isinstance(t, dict) else str(t)
            for t in item.get("tags") or []]
    return {"name": str(item.get("name") or ""),
            "category": str(item.get("category") or ""),
            "tags": " / ".join(tags),
            "description": str(item.get("_desc") or "")}


def candidate_lines(entries, terms=()):
    result = []
    for number, item in enumerate(entries, 1):
        fields = source_fields(item)
        payload = dict(fields, item_id=str(item["id"]),
                       detail_status=item.get("detail_status", "unknown"))
        payload["description"] = excerpt(fields["description"], terms)
        payload["source_hash"] = hashlib.sha256(json.dumps(fields, sort_keys=True,
                                        ensure_ascii=False).encode()).hexdigest()
        result.append(f"{number}. " + json.dumps(payload, ensure_ascii=False))
    return result


def grounded_evaluation(data, entries, required_terms=()):
    """Keep only cited, available evidence. Relatedness remains a model judgement.

    This validates provenance and negative constraints; it does not turn literal
    term mentions into a guarantee of compatibility.
    """
    claims = data.get("evidence")
    claims = claims if isinstance(claims, list) else []
    by_id = {str(item["id"]): (i, item) for i, item in enumerate(entries, 1)}
    accepted = set()
    blocked = set()
    for claim in claims[:24]:
        if not isinstance(claim, dict):
            continue
        pair = by_id.get(str(claim.get("item_id") or ""))
        if not pair:
            continue
        number, item = pair
        fields = source_fields(item)
        quote = str(claim.get("quote") or "").strip()
        field = claim.get("field")
        state = claim.get("status")
        if field not in fields or not quote or quote not in fields[field]:
            continue
        if state == "unsupported" or (state == "related" and claim.get("relation") == "thematic"):
            item["relevance_status"] = "unsupported"
            item["relevance_evidence"] = {"field": field, "quote": quote[:240]}
            if claim.get("relation") == "thematic":
                item["relevance_evidence"]["relation"] = "thematic"
            blocked.add(str(number))
            accepted.discard(str(number))
            continue
        if state != "related" or claim.get("relation") == "unknown" or str(number) in blocked:
            continue
        if required_terms:
            if field != "description" or item.get("detail_status") != "available":
                continue
            statements = [s for s in re.split(r"[。！？\n]", fields["description"])
                          if any(str(term).casefold() in s.casefold() for term in required_terms)]
            if any(NEGATIVE.search(statement) for statement in statements):
                item["relevance_status"] = "unsupported"
                item["relevance_evidence"] = {"field": "description", "quote": next(
                    statement for statement in statements if NEGATIVE.search(statement))[:240]}
                blocked.add(str(number))
                accepted.discard(str(number))
                continue
            if not any(str(term).casefold() in quote.casefold() for term in required_terms):
                continue
        if NEGATIVE.search(quote):
            item["relevance_status"] = "unsupported"
            item["relevance_evidence"] = {"field": field, "quote": quote[:240]}
            blocked.add(str(number))
            accepted.discard(str(number))
            continue
        item["relevance_status"] = "related"
        item["relevance_evidence"] = {"field": field, "quote": quote[:240]}
        accepted.add(str(number))
    hits = [str(hit) for hit in data.get("hits") or [] if str(hit) in accepted]
    for item in entries:
        item.setdefault("relevance_status", "unknown")
    result = dict(data, hits=list(dict.fromkeys(hits))[:6])
    if result.get("verdict") == "ok" and len(result["hits"]) < 3:
        result.update(verdict="retry", reason="带来源的相关证据不足三条，未能确认")
    return result


def evidence_label(item):
    state = item.get("relevance_status")
    if state not in ("related", "unsupported", "unknown"):
        return ""
    labels = {"related": "相关候选", "unsupported": "评估提示不满足要求", "unknown": "相关性未核实"}
    evidence = item.get("relevance_evidence") or {}
    fields = {"name": "商品名", "category": "品类", "tags": "标签", "description": "商品说明"}
    quote = " ".join(str(evidence.get("quote") or "").split())[:160]
    suffix = f" · {fields.get(evidence.get('field'), '来源')}原文：{quote}" if quote else ""
    return labels[state] + suffix


def promote_evidence(entries):
    order = {"related": 0, "unknown": 1, "unsupported": 2}
    return sorted(entries, key=lambda item: order.get(item.get("relevance_status"), 1))


def structured_options(model, *, evaluation=False):
    """Known GLM text-model controls; leave other providers/models unchanged.

    GLM 5.3 cannot disable thinking. Use its documented low effort and enough
    output headroom so reasoning cannot consume the whole JSON allowance.
    """
    model = str(model or "").lower()
    if re.fullmatch(r"glm-5\.3(?:-flash|-[a-z0-9]+)?", model):
        return {"thinking": {"type": "enabled"}, "reasoning_effort": "low",
                "response_format": {"type": "json_object"},
                "max_tokens": 4096 if evaluation else 2000}
    if model in {"glm-5.2", "glm-5.1", "glm-5", "glm-4.7", "glm-4.7-flash",
                 "glm-4.7-flashx", "glm-4.6", "glm-4.5", "glm-4.5-air",
                 "glm-4.5-x", "glm-4.5-airx", "glm-4.5-flash"}:
        return {"thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"}}
    return {}
