"""各结果来源共同遵守的筛选规则，未知信息不能冒充已满足条件。"""


def filter_entries(entries, adult="include", tag=None):
    result = []
    for item in entries:
        if adult == "exclude" and item.get("is_adult") is not False:
            continue
        if adult == "only" and item.get("is_adult") is not True:
            continue
        if tag:
            tags = [t.get("name") if isinstance(t, dict) else t
                    for t in item.get("tags") or []]
            tagged = any(str(t).casefold() == tag.casefold() for t in tags if t)
            # 站内请求显式使用此标签，标签筛选本身也是来源证据。
            searched = (item.get("_search_tag") or "").casefold() == tag.casefold()
            vrchat = tag.casefold() == "vrchat" and item.get("is_vrchat") is True
            if not (tagged or searched or vrchat):
                continue
        result.append(item)
    return result
