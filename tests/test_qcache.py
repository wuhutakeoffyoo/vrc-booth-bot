import sys
import tempfile
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "plugins"))

from booth_search import qcache  # noqa: E402


class TestQCache(unittest.TestCase):
    def setUp(self):
        qcache._conn = None
        self._tmp = tempfile.TemporaryDirectory()
        qcache.DB_PATH = Path(self._tmp.name) / "qc.sqlite3"

    def tearDown(self):
        qcache._conn = None
        self._tmp.cleanup()

    def test_put_get_roundtrip(self):
        payload = {"text": "hello", "entries": [{"id": 1}]}
        qcache.put("k1", payload, ttl=600)
        self.assertEqual(qcache.get("k1", 600), payload)

    def test_ttl_expiry(self):
        qcache.put("k2", {"a": 1}, ttl=10)
        self.assertIsNone(qcache.get("k2", 0))       # ttl=0 → 不命中
        self.assertIsNotNone(qcache.get("k2", 600))  # ttl 足够 → 命中

    def test_capacity_evicts_oldest(self):
        for i in range(5):
            qcache.put(f"cap{i}", {"i": i}, ttl=600, max_entries=3)
        self.assertIsNone(qcache.get("cap0", 600))   # 最旧被清理
        self.assertIsNone(qcache.get("cap1", 600))
        self.assertIsNotNone(qcache.get("cap4", 600))

    def test_expired_rows_purged_on_put(self):
        qcache.put("old", {"a": 1}, ttl=600)
        # 直接把 ts 改老
        qcache._db().execute("UPDATE qcache SET ts = 1 WHERE key = 'old'")
        qcache._db().commit()
        qcache.put("new", {"b": 2}, ttl=600)
        self.assertIsNone(qcache.get("old", 600))    # 过期行被清理


    def test_version_salt_invalidates_old_entries(self):
        key = qcache.make_key("vx")
        qcache.put(key, {"a": 1}, ttl=600)
        self.assertIsNotNone(qcache.get(key, 600))
        old = qcache.CACHE_VERSION
        qcache.CACHE_VERSION = old + "_bump"
        try:
            # 版本升级后 make_key 产出新前缀，旧条目不再命中
            self.assertIsNone(qcache.get(qcache.make_key("vx"), 600))
        finally:
            qcache.CACHE_VERSION = old


if __name__ == "__main__":
    unittest.main()
