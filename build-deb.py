#!/usr/bin/env python3
"""Build dist/logguard_<version>_all.deb without dpkg-deb (pure stdlib).

Works on any OS. The package is architecture-independent (Architecture: all),
so one .deb serves armhf/arm64 Orange Pi boards alike.
"""
import gzip
import hashlib
import io
import os
import re
import sys
import tarfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))

# (source relative to ROOT, path inside package, mode)
FILES = [
    ("logguard.py", "usr/sbin/logguard", 0o755),
    ("logguard.conf", "etc/logguard.conf", 0o644),
    ("systemd/logguard.service", "lib/systemd/system/logguard.service", 0o644),
    ("systemd/logguard.timer", "lib/systemd/system/logguard.timer", 0o644),
    ("README.md", "usr/share/doc/logguard/README.md", 0o644),
    ("LICENSE", "usr/share/doc/logguard/copyright", 0o644),
    ("CHANGELOG.md", "usr/share/doc/logguard/changelog", 0o644),
]
SCRIPTS = ["postinst", "prerm", "postrm"]
CONTROL = """\
Package: logguard
Version: {version}
Architecture: all
Maintainer: {maintainer}
Installed-Size: {size}
Depends: python3 (>= 3.7), systemd
Recommends: logrotate
Section: admin
Priority: optional
Description: keep /var/log from filling up (zram/tmpfs friendly)
 A systemd timer checks the free space (blocks and inodes) of /var/log.
 Below a configurable threshold (default 15%) it moves old rotated logs
 to a persistent archive for 24 hours, then purges them. Escalates to
 journal vacuum and truncating active logs if needed.
"""


def version():
    src = open(os.path.join(ROOT, "logguard.py")).read()
    return re.search(r'__version__ = "([^"]+)"', src).group(1)


def add(tar, name, data, mode, mtime, isdir=False):
    ti = tarfile.TarInfo("./" + name)
    ti.mtime, ti.mode = mtime, mode
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    if isdir:
        ti.type = tarfile.DIRTYPE
    else:
        ti.size = len(data)
    tar.addfile(ti, None if isdir else io.BytesIO(data))


def make_tar(entries, mtime, dirs=()):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=mtime, compresslevel=9) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
            for d in dirs:
                add(tar, d, b"", 0o755, mtime, isdir=True)
            for name, data, mode in entries:
                add(tar, name, data, mode, mtime)
    return buf.getvalue()


def ar_member(name, data, mtime):
    hdr = "%-16s%-12d%-6d%-6d%-8s%-10d`\n" % (name, mtime, 0, 0, "100644", len(data))
    return hdr.encode() + data + (b"\n" if len(data) % 2 else b"")


def main():
    ver = version()
    mtime = int(os.environ.get("SOURCE_DATE_EPOCH", time.time()))
    maintainer = os.environ.get("DEB_MAINTAINER", "mgoreiro <mgoreiro@gmail.com>")

    data_entries, md5s, size = [], [], 0
    for src, dst, mode in FILES:
        data = open(os.path.join(ROOT, src), "rb").read()
        data_entries.append((dst, data, mode))
        md5s.append("%s  %s" % (hashlib.md5(data).hexdigest(), dst))
        size += len(data)

    dirs = set()
    for _, dst, _ in FILES:
        parts = dst.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    data_tar = make_tar(data_entries, mtime, sorted(dirs))

    control = CONTROL.format(version=ver, maintainer=maintainer,
                             size=max(1, (size + 1023) // 1024))
    ctrl_entries = [
        ("control", control.encode(), 0o644),
        ("md5sums", ("\n".join(md5s) + "\n").encode(), 0o644),
        ("conffiles", b"/etc/logguard.conf\n", 0o644),
    ]
    for s in SCRIPTS:
        ctrl_entries.append((s, open(os.path.join(ROOT, "packaging", s), "rb").read(), 0o755))
    control_tar = make_tar(ctrl_entries, mtime)

    out_dir = os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "logguard_%s_all.deb" % ver)
    with open(out, "wb") as fh:
        fh.write(b"!<arch>\n")
        fh.write(ar_member("debian-binary", b"2.0\n", mtime))
        fh.write(ar_member("control.tar.gz", control_tar, mtime))
        fh.write(ar_member("data.tar.gz", data_tar, mtime))
    print(out)


if __name__ == "__main__":
    sys.exit(main())
