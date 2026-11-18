# logguard

[Español](README.es.md)

Keeps `/var/log` from filling up. It is meant for ARM boards (Orange Pi, Raspberry Pi…) running Armbian or Debian where `/var/log` lives in RAM (zram or tmpfs, for example with `armbian-ramlog` or `log2ram`) and is therefore small: when it fills up, services can no longer write their logs.

A systemd timer runs a Python 3 script (standard library only) every minute. If the free space drops below a threshold (15 % by default, set in `/etc/logguard.conf`), it moves the old logs to a persistent folder outside `/var/log`, keeps them for 24 hours and then purges them.

![logguard archiving old logs when /var/log is almost full](https://raw.githubusercontent.com/mgoreiro/logguard/main/docs/demo.gif)

## Install

### From the .deb package (recommended)

Download the `.deb` from the [latest release](https://github.com/mgoreiro/logguard/releases/latest) and install it:

```bash
sudo apt install ./logguard_1.0.5_all.deb
```

It is `Architecture: all`, so it works the same on armhf and arm64. It depends on `python3 (>= 3.7)` and `systemd`, and recommends `logrotate`. On install it enables and starts `logguard.timer`. On upgrade your `/etc/logguard.conf` is kept.

To remove it: `sudo apt remove logguard` (keeps configuration and archive) or `sudo apt purge logguard` (also deletes `/var/lib/logguard`).

### From source

```bash
git clone https://github.com/mgoreiro/logguard.git
cd logguard
sudo ./install.sh              # installs the script, the config (if missing) and the units, and enables the timer
sudo ./install.sh --uninstall
```

## Usage

```bash
logguard --status              # free space and archived batches
logguard -n -v                 # dry run with verbose output
logguard -c /path/other.conf   # use another configuration file
journalctl -u logguard         # history of actions
systemctl status logguard.timer
```

The service writes nothing to the journal while there is nothing to clean.

## How it works

On every run it:

1. **Purges** archived batches older than `RETENTION_HOURS` (24 h), whether or not there is space pressure.
2. Measures the free space of `LOG_DIR` (the **lower** of blocks and inodes). If it is >= `MIN_FREE_PERCENT`, it stops.
3. Otherwise it escalates step by step, stopping as soon as `TARGET_FREE_PERCENT` is reached:
   1. **Rotated logs** (`*.1`, `*.2.gz`, `*.old`, `*-YYYYMMDD`, `*.gz|xz|zst|bz2`) go to the archive, compressed with gzip. Oldest first.
   2. **Stale files**: not rotated but unmodified for `STALE_HOURS`, or per-day sysstat files (`sysstat/saNN`, `sarNN`) from any day other than today, go to the archive.
   3. `journalctl --vacuum-size` (only if `/var/log/journal` exists).
   4. Optional: `logrotate -f` (`LOGROTATE_FORCE=yes`) and archiving what it produces.
   5. **Last resort**: it saves the tail (`TAIL_KB`) of the biggest active text logs and truncates them. Binary files (`lastlog`, `wtmp`, `sa*`…) are never truncated.
4. It exits with code 3 if, after all that, free space is still below the minimum (the service then shows as *failed* in systemd).

Safety guarantees:

- A file open by a process is never moved or truncated (checked in `/proc/*/fd`), except for the last-resort truncation, which only affects text logs.
- The archive keeps each run in its own `ARCHIVE_DIR/<YYYYMMDDTHHMMSSZ>/` directory, so purging does not depend on file mtimes. Directories in the archive that are not named like a batch are never deleted.
- If the archive destination has less than `ARCHIVE_MIN_FREE_MB` free, it does not archive (and the truncation does not save the tail).
- A `flock` on `/run/logguard.lock` prevents overlapping runs.
- It does not run `fstrim` or measure zram's RAM: ext4 reuses deleted blocks even if zram does not give them back, which is all that matters to keep writing.

## Configuration

`/etc/logguard.conf` (`KEY=VALUE` format, `#` for comments):

| Key | Default | Description |
|---|---|---|
| `MIN_FREE_PERCENT` | `15` | Free-space threshold (%) below which it cleans |
| `TARGET_FREE_PERCENT` | `25` | It cleans until free space exceeds this (hysteresis) |
| `LOG_DIR` | `/var/log` | Watched directory |
| `ARCHIVE_DIR` | `/var/lib/logguard/archive` | Temporary archive; must be persistent and outside `/var/log` and `/var/log.hdd` |
| `RETENTION_HOURS` | `24` | Hours each batch is kept |
| `ARCHIVE_MIN_FREE_MB` | `100` | Do not archive if the destination has less free space |
| `COMPRESS` | `gzip` | `gzip` or `none` |
| `JOURNAL_VACUUM_SIZE` | `20M` | For `journalctl --vacuum-size`; empty = skip |
| `LOGROTATE_FORCE` | `no` | Run `logrotate -f` as an extra step |
| `STALE_HOURS` | `24` | Age for archiving non-rotated files; `0` = disable the step |
| `TRUNCATE_ACTIVE` | `yes` | Allow the last-resort truncation |
| `TRUNCATE_MIN_KB` | `64` | Minimum size to consider truncating |
| `TAIL_KB` | `256` | Tail saved before truncating |
| `EXCLUDE` | *(empty)* | Comma-separated fnmatch patterns (relative to `LOG_DIR`) that are never touched, e.g. `sysstat/*` |

## Development

```bash
python3 -m unittest discover -s tests   # unit tests (simulated filesystem, never touches /var/log)
python3 build-deb.py                    # creates dist/logguard_<version>_all.deb
DEB_MAINTAINER="Name <email>" python3 build-deb.py
```

`build-deb.py` builds the `.deb` with the Python standard library only (an `ar` archive plus a `tar.gz`), so it works on any system without `dpkg-deb`. The version is read from `__version__` in `logguard.py`.

Layout:

```
logguard.py          main script (installed as /usr/sbin/logguard)
logguard.conf        default configuration (/etc/logguard.conf)
systemd/             logguard.service and logguard.timer
packaging/           postinst, prerm, postrm of the .deb
build-deb.py         .deb builder
install.sh           install/uninstall without the package
tests/               unit tests
```

## Known limitations

- Truncating an active log is only clean if the process opens it with `O_APPEND` (usual for rsyslog and most daemons). Otherwise a sparse file may remain until the process reopens it.
- With a 1-minute interval, a process that writes hundreds of MB per minute can fill `/var/log` between two runs; in that case raise `MIN_FREE_PERCENT` or fix the source of the noise.
- `armbian-truncate-logs` can coexist as a second line of defence; logguard does not modify it.
- sysstat data from before the current day is archived for 24 h and then lost; use `EXCLUDE=sysstat/*` if you want to keep it.

## Author

**Miguel González Oreiro** · <mgoreiro@gmail.com> · [github.com/mgoreiro](https://github.com/mgoreiro)

## License

[MIT](LICENSE) © 2026 mgoreiro. Change history in [CHANGELOG.md](CHANGELOG.md).
