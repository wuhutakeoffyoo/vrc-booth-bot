"""访问控制纯逻辑（不依赖插件元数据，便于单测）。

规则：
- 群聊：GROUP_WHITELIST 非空时仅白名单群放行（白名单外静默忽略）；
- 私聊：管理员始终放行；非管理员由 allow_private 决定；
- 敏感指令（/vrc r18）：仅管理员；
- 限速：同一用户两次搜索最小间隔 user_cooldown 秒、每分钟最多 user_rate_limit 次。
"""
import time

from .config import Config

# 进程内滑动窗口（重启清零；单进程 bot 足够）
_user_hits: dict = {}
_WINDOW = 60.0


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


def check_rate(cfg: Config, user_id, now=None) -> tuple[bool, int]:
    """按用户限速。放行时记录本次时间；拒绝时返回 (False, 建议等待秒数)。
    now 参数供测试注入时钟。"""
    now = time.monotonic() if now is None else now
    uid = str(user_id)
    hits = [t for t in _user_hits.get(uid, []) if now - t < _WINDOW]
    if len(hits) >= max(1, cfg.user_rate_limit):
        wait = int(_WINDOW - (now - hits[0])) + 1
        return False, max(wait, 1)
    if hits and now - hits[-1] < cfg.user_cooldown:
        wait = int(cfg.user_cooldown - (now - hits[-1])) + 1
        return False, max(wait, 1)
    hits.append(now)
    _user_hits[uid] = hits
    return True, 0
