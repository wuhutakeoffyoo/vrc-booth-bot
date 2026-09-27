"""访问控制纯逻辑（不依赖插件元数据，便于单测）。

规则：
- 群聊：GROUP_WHITELIST 非空时仅白名单群放行（白名单外静默忽略）；
- 私聊：管理员始终放行；非管理员由 allow_private 决定；
- 敏感指令（/vrc r18）：仅管理员。
"""
from .config import Config


def _id_set(values) -> set:
    """把配置里的 QQ/群号统一成字符串集合（兼容 int/str 写法）。"""
    return {str(v).strip() for v in (values or []) if str(v).strip()}


def is_admin(cfg: Config, user_id) -> bool:
    return str(user_id) in _id_set(cfg.admin_users)


def access_ok(cfg: Config, *, user_id, group_id=None) -> bool:
    """群白名单 + 私聊策略综合判定。"""
    if group_id is not None:
        groups = _id_set(cfg.group_whitelist)
        return (not groups) or str(group_id) in groups
    if is_admin(cfg, user_id):
        return True
    return bool(cfg.allow_private)


def sensitive_allowed(cfg: Config, user_id) -> bool:
    """敏感指令（/vrc r18）仅管理员。"""
    return is_admin(cfg, user_id)
