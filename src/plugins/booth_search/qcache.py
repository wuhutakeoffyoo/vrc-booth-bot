"""bot 级查询结果缓存：加速重复查询、省 AI 额度。

sqlite 存储（~/.vrc-booth-bot/query_cache.sqlite3），按 key 缓存完整结果 dict
（JSON 序列化）。TTL 与容量上限可配；写入时自动清理过期与超额最旧条目。
"""
import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path.home() / ".vrc-booth-bot" / "query_cache.sqlite3"
_conn = None

DEFAULT_TTL = 1800      # 30 分钟
DEFAULT_MAX = 300       # 最大缓存条数


def _db():
    global _conn
    if _conn is None:
        try:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(DB_PATH), timeout=3)
            conn.execute("CREATE TABLE IF NOT EXISTS qcache "
                         "(key TEXT PRIMARY KEY, ts REAL, payload TEXT)")
            _conn = conn
        except Exception:
            _conn = False
    return _conn if _conn else None


def make_key(*parts) -> str:
    """缓存 key：各部分小写化拼接（顺序稳定）。"""
    return "|".join(str(p).strip().lower() for p in parts if p is not None)


def get(key: str, ttl: int):
    """取缓存；命中且未过期返回结果 dict，否则 None。"""
    conn = _db()
    if not conn:
        return None
    try:
        row = conn.execute("SELECT ts, payload FROM qcache WHERE key = ?",
                           (key,)).fetchone()
    except sqlite3.Error:
        return None
    if not row or time.time() - row[0] > ttl:
        return None
    try:
        return json.loads(row[1])
    except json.JSONDecodeError:
        return None


def put(key: str, payload: dict, ttl: int = DEFAULT_TTL,
        max_entries: int = DEFAULT_MAX):
    """写入缓存并自动清理：过期条目全删，超出容量删最旧。"""
    conn = _db()
    if not conn:
        return
    now = time.time()
    try:
        conn.execute("INSERT OR REPLACE INTO qcache (key, ts, payload) VALUES (?,?,?)",
                     (key, now, json.dumps(payload, ensure_ascii=False)))
        conn.execute("DELETE FROM qcache WHERE ts < ?", (now - ttl,))
        conn.execute("DELETE FROM qcache WHERE key = ? NOT IN "
                     "(SELECT key FROM qcache ORDER BY ts DESC LIMIT ?)",
                     (key, max_entries))
        conn.commit()
    except sqlite3.Error:
        pass


def clear():
    conn = _db()
    if conn:
        try:
            conn.execute("DELETE FROM qcache")
            conn.commit()
        except sqlite3.Error:
            pass
