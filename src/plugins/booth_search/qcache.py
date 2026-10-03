"""bot 级查询结果缓存：加速重复查询、省 AI 额度。

sqlite 存储（~/.vrc-booth-bot/query_cache.sqlite3），按 key 缓存完整结果 dict
（JSON 序列化）。TTL 与容量上限可配；写入时自动清理过期与超额最旧条目。
"""
import json
import hashlib
import sqlite3
import time
import shutil
import subprocess
from functools import lru_cache
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
                         "(key TEXT PRIMARY KEY, ts REAL, payload TEXT, expires REAL)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(qcache)")}
            if "expires" not in columns:
                conn.execute("ALTER TABLE qcache ADD COLUMN expires REAL")
                conn.execute("UPDATE qcache SET expires=ts+? WHERE expires IS NULL", (DEFAULT_TTL,))
            conn.commit()
            _conn = conn
        except Exception:
            _conn = False
    return _conn if _conn else None


# 结果语义版本：前缀进缓存 key。凡影响结果内容的代码变更（策略/格式/修复）部署时
# 必须递增，让旧缓存整体失效——否则新代码上线后 TTL 内用户拿到的仍是修复前
# 的旧结果（2026-09-30「铃铛」事故：修复已上线，用户却命中旧缓存以为没优化）
CACHE_VERSION = "12"


@lru_cache(maxsize=8)
def semantic_fingerprint(cli_path=""):
    """Startup fingerprint of explicit source files; never hash credentials."""
    digest = hashlib.sha256()
    base = Path(__file__).resolve().parent
    names = ("__init__.py", "config.py", "qcache.py", "booth_client.py", "execution.py",
             "vision.py", "rank.py", "format.py", "forward.py", "result_policy.py",
             "webfind.py", "search_evidence.py", "image_download.py", "provider_api.py", "search_api.py")
    for name in names:
        digest.update(name.encode() + b"\0")
        path = base / name
        digest.update(path.read_bytes() if path.is_file() else b"missing")
    resolved = cli_path or shutil.which("booth") or ""
    parent = Path(resolved).resolve() if resolved else None
    if parent and parent.is_file() and parent.suffix.lower() == ".py":
        for name in ("booth.py", "smart_search.py", "reverse_search.py",
                     "request_budget.py", "search_evidence.py", "provider_api.py", "search_api.py"):
            path = parent.parent / name
            digest.update(name.encode() + b"\0")
            digest.update(path.read_bytes() if path.is_file() else b"missing")
    elif parent and parent.is_file():
        # Installed wrapper/symlink may remain unchanged across releases.
        try:
            proc = subprocess.run([str(parent), "bot", '{"action":"version"}'],
                                  capture_output=True, text=True, encoding="utf-8", timeout=15)
            data = json.loads(proc.stdout.strip().splitlines()[-1]).get("data") or {}
            identity = data.get("semantic_fingerprint")
            if not identity:
                raise ValueError("CLI semantic identity unavailable")
            digest.update(str(identity).encode())
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            # No stable CLI identity: this process cannot reuse persistent keys
            # from another startup. Normal execution will report CLI errors.
            digest.update(str(time.time_ns()).encode())
    else:
        digest.update(b"cli-unavailable")
    return digest.hexdigest()


def make_key(*parts) -> str:
    """缓存 key：版本前缀 + 各部分小写化拼接（顺序稳定）。"""
    return CACHE_VERSION + "|" + "|".join(str(p).strip().lower() for p in parts if p is not None)


def configuration_key(cfg) -> str:
    """只摘要影响结果的非敏感配置，不写入 key 或认证信息。"""
    names = ("booth_limit", "booth_sort", "vrc_tag", "r18_mode", "ai_mode",
             "vision_model", "fallback_model", "ai_cli_model", "recall_enabled",
             "websearch_fallback", "booth_cli_path", "vision_base_url",
             "fallback_base_url", "ai_cli_bin", "exa_base_url")
    data = {name: getattr(cfg, name) for name in names}
    for name in ("request_budget", "retry_request_budget", "query_timeout",
                 "search_candidate_limit", "plan_cache_ttl", "run_profile", "benchmark_allow_ai",
                 "search_base_url", "search_provider", "search_ddg_enabled"):
        data[name] = getattr(cfg, name, None)
    data["semantic"] = semantic_fingerprint(cfg.booth_cli_path)
    data["primary_available"] = bool(cfg.vision_api_key)
    data["fallback_available"] = bool(cfg.fallback_api_key)
    data["exa_available"] = bool(cfg.exa_api_key)
    data["search_available"] = bool(getattr(cfg, "search_api_key", ""))
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def get(key: str, ttl: int):
    """取缓存；命中且未过期返回结果 dict，否则 None。"""
    conn = _db()
    if not conn:
        return None
    try:
        row = conn.execute("SELECT ts, payload, expires FROM qcache WHERE key = ?",
                           (key,)).fetchone()
    except sqlite3.Error:
        return None
    if not row or time.time() - row[0] > ttl or (row[2] is not None and row[2] < time.time()):
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
        conn.execute("INSERT OR REPLACE INTO qcache (key, ts, payload, expires) VALUES (?,?,?,?)",
                     (key, now, json.dumps(payload, ensure_ascii=False), now+ttl))
        conn.execute("DELETE FROM qcache WHERE expires < ?", (now,))
        conn.execute("DELETE FROM qcache WHERE key NOT IN "
                     "(SELECT key FROM qcache ORDER BY ts DESC LIMIT ?)",
                     (max_entries,))
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
