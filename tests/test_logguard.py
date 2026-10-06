import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import logguard  # noqa: E402

CAP = 1000 * 1024  # simulated capacity of the log filesystem


def dir_size(path):
    return sum(os.path.getsize(os.path.join(r, f))
               for r, _, fs in os.walk(path) for f in fs)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = os.path.join(self.tmp.name, "log")
        self.arch = os.path.join(self.tmp.name, "archive")
        os.makedirs(self.logs)
        self.cfg = logguard.Config({
            "LOG_DIR": self.logs, "ARCHIVE_DIR": self.arch,
            "ARCHIVE_MIN_FREE_MB": "1", "TRUNCATE_MIN_KB": "1",
        })

    def write(self, name, kb, binary=False, age_h=0):
        p = os.path.join(self.logs, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        data = os.urandom(kb * 1024) if binary else os.urandom(kb * 512).hex().encode()
        with open(p, "wb") as f:
            f.write(data)
        if age_h:
            t = time.time() - age_h * 3600
            os.utime(p, (t, t))
        return p

    def fake_free(self, _path):
        return 100.0 - dir_size(self.logs) * 100.0 / CAP

    def cleaner(self, **kw):
        return logguard.Cleaner(self.cfg, free_fn=self.fake_free, **kw)

    def batches(self):
        return sorted(os.listdir(self.arch)) if os.path.isdir(self.arch) else []


class TestRotatedRegex(unittest.TestCase):
    def test_matches(self):
        for n in ("syslog.1", "syslog.2.gz", "a.old", "syslog-20261006",
                  "syslog-20261006.gz", "x.log.gz", "daemon.log.10"):
            self.assertTrue(logguard.is_rotated(n), n)

    def test_non_matches(self):
        for n in ("syslog", "kern.log", "auth.log", "Xorg.0.log"):
            self.assertFalse(logguard.is_rotated(n), n)


class TestConfig(unittest.TestCase):
    def test_load_and_defaults(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
            f.write("# c\nMIN_FREE_PERCENT=10 # inline\nRETENTION_HOURS=2\n")
            f.flush()
            cfg = logguard.load_config(f.name)
        self.assertEqual(cfg.min_free, 10)
        self.assertEqual(cfg.retention_s, 7200)
        self.assertEqual(cfg.target_free, 25)

    def test_target_never_below_min(self):
        cfg = logguard.Config({"MIN_FREE_PERCENT": "30", "TARGET_FREE_PERCENT": "20"})
        self.assertEqual(cfg.target_free, 30)


class TestCleaner(Base):
    def test_nothing_when_enough_space(self):
        self.write("syslog", 100)
        self.write("syslog.1", 100)
        self.assertEqual(self.cleaner().run(), 0)
        self.assertEqual(self.batches(), [])
        self.assertTrue(os.path.exists(os.path.join(self.logs, "syslog.1")))

    def test_archives_rotated_first(self):
        self.write("syslog", 100)                  # active, must survive
        for i in range(1, 6):
            self.write("syslog.%d" % i, 160)       # 900K total -> 10% free
        self.assertEqual(self.cleaner().run(), 0)
        self.assertTrue(os.path.getsize(os.path.join(self.logs, "syslog")) > 0)
        self.assertTrue(self.fake_free(None) >= 25)
        self.assertEqual(len(self.batches()), 1)
        archived = os.listdir(os.path.join(self.arch, self.batches()[0]))
        self.assertTrue(all(a.endswith(".gz") for a in archived))

    def test_stops_at_target(self):
        for i in range(1, 9):
            self.write("big.%d" % i, 110)          # 880K -> 12% free
        self.cleaner().run()
        remaining = [f for f in os.listdir(self.logs)]
        self.assertTrue(remaining)                 # not everything archived

    def test_truncates_active_when_no_rotated(self):
        p = self.write("huge.log", 900)
        self.assertEqual(self.cleaner().run(), 0)
        self.assertEqual(os.path.getsize(p), 0)
        b = os.path.join(self.arch, self.batches()[0])
        self.assertTrue(os.path.exists(os.path.join(b, "huge.log.tail.gz")))

    def test_stale_files_are_archived_not_truncated(self):
        # sysstat style: previous days' files without rotation suffix
        for d in range(1, 6):
            self.write("sysstat/sa%02d" % d, 190, binary=True, age_h=48)
        today = self.write("sysstat/sa06", 10, binary=True)
        self.assertEqual(self.cleaner().run(), 0)
        self.assertTrue(os.path.exists(today))
        b = os.path.join(self.arch, self.batches()[0], "sysstat")
        self.assertTrue(all(f.endswith(".gz") for f in os.listdir(b)))
        self.assertTrue(len(os.listdir(b)) >= 2)

    def test_sysstat_other_days_archived_even_if_fresh(self):
        today = time.localtime().tm_mday
        others = [d for d in range(1, 32) if d != today][:5]
        for d in others:                      # fresh mtime, like after midnight job
            self.write("sysstat/sa%02d" % d, 190, binary=True)
        keep = self.write("sysstat/sar%02d" % today, 10)
        self.assertEqual(self.cleaner().run(), 0)
        self.assertTrue(os.path.exists(keep))
        b = os.path.join(self.arch, self.batches()[0], "sysstat")
        self.assertTrue(len(os.listdir(b)) >= 2)

    def test_binary_files_never_truncated(self):
        p = self.write("lastlog", 950, binary=True)   # fresh, binary
        self.assertEqual(self.cleaner().run(), 3)
        self.assertEqual(os.path.getsize(p), 950 * 1024)

    def test_open_files_are_not_moved(self):
        p = self.write("syslog.1", 950)
        orig = logguard.open_files
        logguard.open_files = lambda: {os.path.realpath(p)}
        self.addCleanup(setattr, logguard, "open_files", orig)
        self.cleaner().run()
        self.assertTrue(os.path.exists(p))

    def test_stale_disabled(self):
        self.cfg = logguard.Config({
            "LOG_DIR": self.logs, "ARCHIVE_DIR": self.arch,
            "ARCHIVE_MIN_FREE_MB": "1", "STALE_HOURS": "0",
            "TRUNCATE_ACTIVE": "no"})
        p = self.write("sysstat/sa01", 950, binary=True, age_h=48)
        self.assertEqual(self.cleaner().run(), 3)
        self.assertTrue(os.path.exists(p))

    def test_exclude(self):
        self.cfg = logguard.Config({
            "LOG_DIR": self.logs, "ARCHIVE_DIR": self.arch,
            "ARCHIVE_MIN_FREE_MB": "1", "EXCLUDE": "keep*"})
        keep = self.write("keep.log.1", 900)
        self.assertEqual(self.cleaner().run(), 3)  # cannot free anything
        self.assertTrue(os.path.exists(keep))

    def test_dry_run_touches_nothing(self):
        for i in range(1, 6):
            self.write("syslog.%d" % i, 190)
        before = dir_size(self.logs)
        self.cleaner(dry_run=True).run()
        self.assertEqual(dir_size(self.logs), before)
        self.assertEqual(self.batches(), [])

    def test_dry_run_does_not_truncate_what_it_archived(self):
        today = time.localtime().tm_mday
        d = [x for x in range(1, 32) if x != today][0]
        self.write("sysstat/sar%02d" % d, 950)
        c = self.cleaner(dry_run=True)
        with self.assertLogs("logguard", "INFO") as cm:
            c.run()
        self.assertFalse(any("truncate" in m for m in cm.output))

    def test_archive_without_room_is_skipped(self):
        self.cfg.archive_min_free_mb = 10 ** 9
        self.write("syslog.1", 950)
        self.cleaner().run()
        self.assertTrue(os.path.exists(os.path.join(self.logs, "syslog.1")))


class TestPurge(Base):
    def make_batch(self, age_h):
        ts = datetime.now(timezone.utc) - timedelta(hours=age_h)
        d = os.path.join(self.arch, ts.strftime(logguard.BATCH_FORMAT))
        os.makedirs(d)
        open(os.path.join(d, "f.gz"), "wb").close()
        return os.path.basename(d)

    def test_purges_only_expired_batches(self):
        old = self.make_batch(25)
        new = self.make_batch(2)
        foreign = os.path.join(self.arch, "not-a-batch")
        os.makedirs(foreign)
        logguard.purge_expired(self.cfg)
        left = os.listdir(self.arch)
        self.assertNotIn(old, left)
        self.assertIn(new, left)
        self.assertIn("not-a-batch", left)

    def test_purge_runs_even_without_pressure(self):
        old = self.make_batch(30)
        self.assertEqual(self.cleaner().run(), 0)
        self.assertNotIn(old, self.batches())


if __name__ == "__main__":
    unittest.main()
