#!/usr/bin/env python3
"""logguard: keeps /var/log from filling up (e.g. when it lives on zram/tmpfs).

Run periodically by a systemd timer. When free space (blocks or inodes) of
LOG_DIR drops below MIN_FREE_PERCENT, old logs are moved to a persistent
archive directory and escalating cleanup steps run until TARGET_FREE_PERCENT
is reached. Archived batches are purged after RETENTION_HOURS.

Author: mgoreiro <mgoreiro@gmail.com> - License: MIT
"""
from __future__ import annotations

import argparse
import fcntl
import fnmatch
import glob
import gzip
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

__version__ = "1.0.4"
DEFAULT_CONFIG = "/etc/logguard.conf"
LOCK_FILE = "/run/logguard.lock"
BATCH_FORMAT = "%Y%m%dT%H%M%SZ"
COMPRESSED_EXT = (".gz", ".xz", ".zst", ".bz2")

DEFAULTS = {
    "MIN_FREE_PERCENT": "15",
    "TARGET_FREE_PERCENT": "25",
    "LOG_DIR": "/var/log",
    "ARCHIVE_DIR": "/var/lib/logguard/archive",
    "RETENTION_HOURS": "24",
    "ARCHIVE_MIN_FREE_MB": "100",
    "COMPRESS": "gzip",
    "JOURNAL_VACUUM_SIZE": "20M",
    "LOGROTATE_FORCE": "no",
    "STALE_HOURS": "24",
    "TRUNCATE_ACTIVE": "yes",
    "TRUNCATE_MIN_KB": "64",
    "TAIL_KB": "256",
    "EXCLUDE": "",
}

# syslog.1, syslog.2.gz, foo.old, syslog-20261006, syslog-20261006.gz, foo.gz
ROTATED_RE = re.compile(
    r"(\.\d+|\.old|-\d{8,})(\.(gz|xz|zst|bz2))?$|\.(gz|xz|zst|bz2)$"
)

# sysstat keeps one file per day of month (sa01..sa31, sar01..sar31); the
# midnight job touches them all, so mtime cannot tell old days from today.
DAY_FILE_RE = re.compile(r"^sysstat/sar?(\d\d)$")

log = logging.getLogger("logguard")


class Config:
    def __init__(self, values):
        v = dict(DEFAULTS)
        v.update(values)
        self.min_free = float(v["MIN_FREE_PERCENT"])
        self.target_free = max(float(v["TARGET_FREE_PERCENT"]), self.min_free)
        self.log_dir = v["LOG_DIR"]
        self.archive_dir = v["ARCHIVE_DIR"]
        self.retention_s = float(v["RETENTION_HOURS"]) * 3600
        self.archive_min_free_mb = float(v["ARCHIVE_MIN_FREE_MB"])
        self.compress = v["COMPRESS"].lower() == "gzip"
        self.journal_vacuum = v["JOURNAL_VACUUM_SIZE"]
        self.logrotate_force = _truthy(v["LOGROTATE_FORCE"])
        self.stale_s = float(v["STALE_HOURS"]) * 3600
        self.truncate_active = _truthy(v["TRUNCATE_ACTIVE"])
        self.truncate_min = int(v["TRUNCATE_MIN_KB"]) * 1024
        self.tail_bytes = int(v["TAIL_KB"]) * 1024
        self.exclude = [p.strip() for p in v["EXCLUDE"].split(",") if p.strip()]
        if not 0 < self.min_free < 100:
            raise ValueError("MIN_FREE_PERCENT must be between 0 and 100")


def _truthy(s):
    return s.strip().lower() in ("1", "yes", "true", "on")


def load_config(path):
    values = {}
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.split(" #")[0].strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    else:
        log.warning("config %s not found, using defaults", path)
    return Config(values)


def free_percent(path):
    """Lowest of free-blocks% and free-inodes% (whichever runs out first)."""
    st = os.statvfs(path)
    blocks = st.f_bavail * 100.0 / st.f_blocks if st.f_blocks else 100.0
    inodes = st.f_favail * 100.0 / st.f_files if st.f_files else 100.0
    return min(blocks, inodes)


def open_files():
    """Real paths of regular files currently open by any process."""
    found = set()
    for fd in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            found.add(os.path.realpath(fd))
        except OSError:
            pass
    return found


def looks_binary(path):
    try:
        with open(path, "rb") as fh:
            return b"\0" in fh.read(4096)
    except OSError:
        return True


def is_rotated(name):
    return bool(ROTATED_RE.search(name))


class Archive:
    """Archive of batches: ARCHIVE_DIR/<UTC timestamp>/<relative path>."""

    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        self.dry = dry_run
        self._batch = None

    def has_room(self):
        try:
            os.makedirs(self.cfg.archive_dir, exist_ok=True)
            st = os.statvfs(self.cfg.archive_dir)
        except OSError as e:
            log.error("archive dir unusable: %s", e)
            return False
        free_mb = st.f_bavail * st.f_frsize / 1048576
        if free_mb < self.cfg.archive_min_free_mb:
            log.error("archive has only %.0f MB free (< %.0f)", free_mb,
                      self.cfg.archive_min_free_mb)
            return False
        return True

    def batch(self):
        if self._batch is None:
            name = datetime.now(timezone.utc).strftime(BATCH_FORMAT)
            self._batch = os.path.join(self.cfg.archive_dir, name)
            if not self.dry:
                os.makedirs(self._batch, exist_ok=True)
        return self._batch

    def _dest(self, rel, compress):
        dest = os.path.join(self.batch(), rel)
        if compress and not rel.endswith(COMPRESSED_EXT):
            dest += ".gz"
        return dest

    def move(self, src, rel):
        """Move src into the archive, compressing if configured."""
        dest = self._dest(rel, self.cfg.compress)
        if self.dry:
            log.info("[dry-run] archive %s -> %s", src, dest)
            return True
        try:
            st = os.stat(src)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if dest.endswith(".gz") and not src.endswith(".gz"):
                with open(src, "rb") as fi, gzip.open(dest, "wb", 6) as fo:
                    shutil.copyfileobj(fi, fo)
                os.unlink(src)
            else:
                shutil.move(src, dest)
            os.utime(dest, (st.st_atime, st.st_mtime))
            log.info("archived %s", src)
            return True
        except OSError as e:
            log.error("cannot archive %s: %s", src, e)
            if os.path.exists(dest) and os.path.exists(src):
                os.unlink(dest)
            return False

    def save_tail(self, src, rel, data):
        dest = self._dest(rel + ".tail", self.cfg.compress)
        if self.dry:
            log.info("[dry-run] save tail of %s -> %s", src, dest)
            return True
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            opener = gzip.open if dest.endswith(".gz") else open
            with opener(dest, "wb") as fo:
                fo.write(data)
            return True
        except OSError as e:
            log.error("cannot save tail of %s: %s", src, e)
            return False


def purge_expired(cfg, dry_run=False, now=None):
    """Delete archive batches older than RETENTION_HOURS."""
    now = now if now is not None else time.time()
    if not os.path.isdir(cfg.archive_dir):
        return 0
    purged = 0
    for entry in sorted(os.listdir(cfg.archive_dir)):
        path = os.path.join(cfg.archive_dir, entry)
        try:
            ts = datetime.strptime(entry, BATCH_FORMAT).replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue  # not one of our batches: never touch it
        if os.path.isdir(path) and now - ts >= cfg.retention_s:
            if dry_run:
                log.info("[dry-run] purge %s", path)
            else:
                shutil.rmtree(path, ignore_errors=True)
                log.info("purged expired batch %s", entry)
            purged += 1
    return purged


def iter_logs(cfg):
    """Yield (abs_path, rel_path) of regular files under LOG_DIR."""
    archive = os.path.realpath(cfg.archive_dir)
    for root, dirs, files in os.walk(cfg.log_dir):
        dirs[:] = [d for d in dirs
                   if d != "journal"
                   and os.path.realpath(os.path.join(root, d)) != archive]
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, cfg.log_dir)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            if any(fnmatch.fnmatch(rel, pat) for pat in cfg.exclude):
                continue
            yield p, rel


class Cleaner:
    def __init__(self, cfg, dry_run=False, free_fn=free_percent):
        self.cfg = cfg
        self.dry = dry_run
        self.free_fn = free_fn
        self.archive = Archive(cfg, dry_run)
        self.archive_ok = None
        self.simulated = set()  # dry-run: files already "handled" by an earlier step

    def free(self):
        return self.free_fn(self.cfg.log_dir)

    def _archive_ready(self):
        if self.archive_ok is None:
            self.archive_ok = self.archive.has_room()
        return self.archive_ok

    def _satisfied(self):
        return not self.dry and self.free() >= self.cfg.target_free

    # --- escalating steps -------------------------------------------------
    def _archive_oldest_first(self, files):
        if not self._archive_ready():
            return
        busy = open_files()
        files = [(p, r) for p, r in files
                 if os.path.realpath(p) not in busy and p not in self.simulated]
        files.sort(key=lambda x: os.path.getmtime(x[0]))
        for p, r in files:
            if self.archive.move(p, r) and self.dry:
                self.simulated.add(p)
            if self._satisfied():
                return

    def step_archive_rotated(self):
        self._archive_oldest_first(
            [(p, r) for p, r in iter_logs(self.cfg) if is_rotated(r)])

    def step_archive_stale(self):
        """Files nobody is writing to: not modified for STALE_HOURS, or sysstat's
        per-day files (saNN/sarNN) of any day other than today. Open files are
        never moved."""
        if self.cfg.stale_s <= 0:
            return
        limit = time.time() - self.cfg.stale_s
        today = time.localtime().tm_mday

        def stale(p, r):
            if is_rotated(r):
                return False
            m = DAY_FILE_RE.match(r)
            if m and int(m.group(1)) != today:
                return True
            return os.path.getmtime(p) < limit

        self._archive_oldest_first(
            [(p, r) for p, r in iter_logs(self.cfg) if stale(p, r)])

    def step_journal(self):
        if not self.cfg.journal_vacuum or not shutil.which("journalctl"):
            return
        if not os.path.isdir(os.path.join(self.cfg.log_dir, "journal")):
            return
        self._run(["journalctl", "--vacuum-size=" + self.cfg.journal_vacuum])

    def step_logrotate(self):
        if not self.cfg.logrotate_force or not shutil.which("logrotate"):
            return
        self._run(["logrotate", "-f", "/etc/logrotate.conf"])
        self.step_archive_rotated()

    def step_truncate_active(self):
        if not self.cfg.truncate_active:
            return
        files = []
        for p, r in iter_logs(self.cfg):
            if is_rotated(r):
                continue
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if (size >= self.cfg.truncate_min and p not in self.simulated
                    and not looks_binary(p)):
                files.append((size, p, r))
        for size, p, r in sorted(files, reverse=True):
            if self._archive_ready():
                try:
                    with open(p, "rb") as fh:
                        fh.seek(max(0, size - self.cfg.tail_bytes))
                        tail = fh.read()
                except OSError:
                    tail = b""
                self.archive.save_tail(p, r, tail)
            if self.dry:
                log.info("[dry-run] truncate %s (%d bytes)", p, size)
                continue
            try:
                os.truncate(p, 0)
                log.warning("truncated active log %s (%d bytes)", p, size)
            except OSError as e:
                log.error("cannot truncate %s: %s", p, e)
                continue
            if self._satisfied():
                return

    def _run(self, cmd):
        if self.dry:
            log.info("[dry-run] %s", " ".join(cmd))
            return
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=120)
            log.info("%s -> rc=%d", " ".join(cmd), r.returncode)
        except (OSError, subprocess.SubprocessError) as e:
            log.error("%s failed: %s", " ".join(cmd), e)

    def run(self):
        """Returns 0 when OK, 3 if still below the minimum after cleaning."""
        purge_expired(self.cfg, self.dry)
        free = self.free()
        if free >= self.cfg.min_free:
            log.debug("%.1f%% free in %s: nothing to do", free, self.cfg.log_dir)
            return 0
        log.warning("%.1f%% free in %s (< %.0f%%): cleaning up to %.0f%%",
                    free, self.cfg.log_dir, self.cfg.min_free,
                    self.cfg.target_free)
        for step in (self.step_archive_rotated, self.step_archive_stale,
                     self.step_journal,
                     self.step_logrotate, self.step_truncate_active):
            if self._satisfied():
                break
            step()
        free = self.free()
        if self.dry:
            return 0
        if free < self.cfg.min_free:
            log.error("still only %.1f%% free in %s after cleanup", free,
                      self.cfg.log_dir)
            return 3
        log.info("cleanup done: %.1f%% free", free)
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="show what would be done without touching anything")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--status", action="store_true",
                    help="print free space and archive batches, then exit")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s", stream=sys.stderr)
    cfg = load_config(args.config)

    if args.status:
        print("%s: %.1f%% free (min %.0f%%, target %.0f%%)" % (
            cfg.log_dir, free_percent(cfg.log_dir), cfg.min_free,
            cfg.target_free))
        for b in sorted(glob.glob(os.path.join(cfg.archive_dir, "*"))):
            print("  batch", os.path.basename(b))
        return 0

    lock = open(LOCK_FILE if os.access(os.path.dirname(LOCK_FILE), os.W_OK)
                else os.devnull, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("another instance is running")
        return 0
    return Cleaner(cfg, args.dry_run).run()


if __name__ == "__main__":
    sys.exit(main())
