#!/usr/bin/env python3
"""
monomi collector.

Runs as a systemd service. Every INTERVAL seconds it bundles a snapshot of
system telemetry (most of it cribbed off pironman5's local API since it
already does the heavy lifting) and POSTs it to a Cloudflare Worker.

Designed to keep working even if pironman5 hiccups: any optional source that
raises an exception is silently dropped, the rest of the payload still ships.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any


# ---------------------------------------------------------------------------
# Configuration (env-driven, sane defaults for systemd EnvironmentFile)
# ---------------------------------------------------------------------------

INGEST_URL = os.environ.get(
    "INGEST_URL", "http://localhost:8787/ingest"
)
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "dev-token")
INTERVAL = float(os.environ.get("INTERVAL", "1.0"))
PROC_LIMIT = int(os.environ.get("PROC_LIMIT", "40"))
PIRONMAN_URL = os.environ.get(
    "PIRONMAN_URL", "http://127.0.0.1:34001/api/v1.0/get-data"
)
NUT_UPS = os.environ.get("NUT_UPS", "eaton3s")
HOSTNAME = socket.gethostname()

# Services we care about for the status page panel.
WATCHED_SERVICES = [
    "ssh",
    "docker",
    "nginx",
    "smbd",
    "openmediavault-engined",
    "pironman5",
    "fail2ban",
    "nut-server",
    "nut-monitor",
    "chrony",
    "monit",
    "influxdb",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: float = 3.0) -> str:
    """Run a command, return stdout, raise on failure."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    ).stdout


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------


def collect_pironman() -> dict[str, Any]:
    """Most CPU/mem/disk/net stats — pironman5 already computes deltas for us."""
    with urllib.request.urlopen(PIRONMAN_URL, timeout=2) as r:
        payload = json.load(r)
    return payload.get("data", {}) if payload.get("status") else {}


def collect_ups() -> dict[str, Any]:
    """`upsc <name>` returns key: value lines."""
    out = _run(["/usr/bin/upsc", NUT_UPS])
    result: dict[str, Any] = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        # numeric where possible
        try:
            result[k.strip()] = float(v) if "." in v else int(v)
        except ValueError:
            result[k.strip()] = v
    return result


def collect_throttle() -> dict[str, Any]:
    """vcgencmd: throttled flag + voltage."""
    throttled_raw = _run(["/usr/bin/vcgencmd", "get_throttled"]).strip()
    volt_raw = _run(["/usr/bin/vcgencmd", "measure_volts"]).strip()
    # throttled=0x50005
    value = int(throttled_raw.split("=")[1], 16) if "=" in throttled_raw else 0
    return {
        "raw": throttled_raw,
        "value": value,
        "currently_throttled": bool(value & 0x4),
        "under_voltage_now": bool(value & 0x1),
        "freq_capped_now": bool(value & 0x2),
        "soft_temp_limit_now": bool(value & 0x8),
        "throttled_since_boot": bool(value & 0x40000),
        "under_voltage_since_boot": bool(value & 0x10000),
        "voltage": volt_raw.split("=")[1] if "=" in volt_raw else volt_raw,
    }


def collect_uptime() -> dict[str, Any]:
    with open("/proc/uptime") as f:
        up = float(f.read().split()[0])
    with open("/proc/loadavg") as f:
        parts = f.read().split()
    return {
        "uptime_seconds": up,
        "load_1": float(parts[0]),
        "load_5": float(parts[1]),
        "load_15": float(parts[2]),
    }


def collect_meminfo() -> dict[str, Any]:
    """Parse /proc/meminfo for the full breakdown btop shows (cached, buffers,
    available, swap)."""
    out: dict[str, Any] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                parts = v.strip().split()
                if not parts:
                    continue
                try:
                    val_kb = int(parts[0])
                except ValueError:
                    continue
                out[k.strip()] = val_kb * 1024   # bytes
    except Exception:
        return {}
    # roll up into the keys the UI wants
    total  = out.get("MemTotal", 0)
    free   = out.get("MemFree", 0)
    avail  = out.get("MemAvailable", 0)
    cached = out.get("Cached", 0) + out.get("SReclaimable", 0)
    buffers = out.get("Buffers", 0)
    used   = max(0, total - free - cached - buffers)
    sw_total = out.get("SwapTotal", 0)
    sw_free  = out.get("SwapFree", 0)
    sw_used  = max(0, sw_total - sw_free)
    return {
        "total":     total,
        "used":      used,
        "free":      free,
        "available": avail,
        "cached":    cached,
        "buffers":   buffers,
        "swap_total": sw_total,
        "swap_used":  sw_used,
        "swap_free":  sw_free,
    }


def collect_processes(limit: int = PROC_LIMIT) -> list[dict[str, Any]]:
    """Top N processes sorted by CPU %, parsed from `ps` output. Includes ppid
    so the UI can render the parent/child tree like btop does."""
    try:
        out = subprocess.run(
            [
                "/usr/bin/ps",
                "axo",
                "pid,ppid,user:16,pcpu,pmem,rss,nlwp,comm,args",
                "--sort=-pcpu",
                "--no-headers",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        try:
            rows.append({
                "pid":     int(parts[0]),
                "ppid":    int(parts[1]),
                "user":    parts[2],
                "cpu":     float(parts[3]),
                "mem":     float(parts[4]),
                "rss_kb":  int(parts[5]),
                "threads": int(parts[6]),
                "program": parts[7],
                "cmd":     parts[8],
            })
        except ValueError:
            continue
        if len(rows) >= limit:
            break
    return rows


# cache of last-seen rx/tx byte counters so we can compute per-interface rates
# without waiting on pironman's system-wide rollup.
_iface_last: dict[str, dict[str, float]] = {}


def _read_proc_net_dev(path: str) -> dict[str, tuple[int, int]]:
    """Parse a /proc/net/dev style file. Returns {iface: (rx_bytes, tx_bytes)}."""
    out: dict[str, tuple[int, int]] = {}
    try:
        with open(path) as f:
            for line in f.readlines()[2:]:
                if ":" not in line:
                    continue
                name, rest = line.split(":", 1)
                fields = rest.split()
                if len(fields) < 9:
                    continue
                try:
                    out[name.strip()] = (int(fields[0]), int(fields[8]))
                except ValueError:
                    continue
    except Exception:
        return {}
    return out


def _delta_rate(iface: str, rx: int, tx: int, now: float) -> tuple[float | None, float | None]:
    """Convert raw byte counters into Bytes/s using the previous sample's
    counter. Returns (down_Bps, up_Bps) — None on the first call."""
    prev = _iface_last.get(iface)
    _iface_last[iface] = {"rx": rx, "tx": tx, "ts": now}
    if not prev:
        return None, None
    dt = now - prev["ts"]
    if dt <= 0:
        return None, None
    drx = max(0, rx - prev["rx"])
    dtx = max(0, tx - prev["tx"])
    return drx / dt, dtx / dt


def collect_interface_rates() -> dict[str, dict[str, float | None]]:
    """Per-interface byte-rate sample. eth0 + wlan0 come from the host's
    /proc/net/dev. wg0 comes from inside pia-wg's network namespace
    (the container holds the wireguard tunnel, the host can't see it)."""
    now = time.time()
    rates: dict[str, dict[str, float | None]] = {}

    host = _read_proc_net_dev("/proc/net/dev")
    if "eth0" in host:
        rx, tx = host["eth0"]
        d, u = _delta_rate("eth0", rx, tx, now)
        rates["eth0"] = {"down_Bps": d, "up_Bps": u, "rx_bytes": rx, "tx_bytes": tx}

    # wg0 lives inside the pia-wg container's net namespace
    try:
        pid_raw = subprocess.run(
            ["/usr/bin/docker", "inspect", "-f", "{{.State.Pid}}", "pia-wg"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        if pid_raw and pid_raw.isdigit():
            ns_dev = _read_proc_net_dev(f"/proc/{pid_raw}/net/dev")
            if "wg0" in ns_dev:
                rx, tx = ns_dev["wg0"]
                d, u = _delta_rate("wg0", rx, tx, now)
                rates["wg0"] = {"down_Bps": d, "up_Bps": u, "rx_bytes": rx, "tx_bytes": tx}
    except Exception:
        pass

    return rates


# previous-sample cache for disk I/O rate calc
_disk_io_last: dict[str, float] = {}


# Devices that back the /srv/pool btrfs filesystem (one partition of
# nvme0n1, the entire nvme1n1). This stays out of "/" and "/boot/firmware"
# activity so the i/o graph reflects only the pool members.
POOL_DEVICES = ("nvme0n1p3", "nvme1n1")


def collect_disk_io_rates() -> dict[str, float | None]:
    """Read/write byte rates for the SSD pool members only — i.e. just
    the partitions/disks that make up /srv/pool. /, /boot/firmware
    and other unrelated I/O are excluded. Returns Bps deltas computed
    from the previous sample."""
    now = time.time()
    total_r = 0
    total_w = 0
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 14:
                    continue
                if parts[2] not in POOL_DEVICES:
                    continue
                read_sectors  = int(parts[5])
                write_sectors = int(parts[9])
                total_r += read_sectors  * 512
                total_w += write_sectors * 512
    except Exception:
        return {"read_Bps": None, "write_Bps": None}

    prev_r  = _disk_io_last.get("read")
    prev_w  = _disk_io_last.get("write")
    prev_ts = _disk_io_last.get("ts")
    _disk_io_last["read"]  = total_r
    _disk_io_last["write"] = total_w
    _disk_io_last["ts"]    = now
    if prev_r is None or prev_ts is None:
        return {"read_Bps": None, "write_Bps": None}
    dt = now - prev_ts
    if dt <= 0:
        return {"read_Bps": None, "write_Bps": None}
    return {
        "read_Bps":  max(0.0, (total_r - prev_r) / dt),
        "write_Bps": max(0.0, (total_w - prev_w) / dt),
    }


def collect_diskio() -> dict[str, dict[str, int]]:
    """Per-device read/write byte counters from /proc/diskstats."""
    out: dict[str, dict[str, int]] = {}
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 14:
                    continue
                name = parts[2]
                # filter to whole disks, not partitions
                if not (name.startswith("nvme") or name.startswith("sd") or name.startswith("mmcblk")):
                    continue
                read_sectors  = int(parts[5])
                write_sectors = int(parts[9])
                # Linux sector = 512 bytes
                out[name] = {
                    "read_bytes":  read_sectors * 512,
                    "write_bytes": write_sectors * 512,
                }
    except Exception:
        pass
    return out


def collect_services() -> list[dict[str, Any]]:
    """systemctl is-active for our watch list. Use --quiet style return codes
    via show so we get more detail in one call per unit."""
    result = []
    for name in WATCHED_SERVICES:
        try:
            out = subprocess.run(
                [
                    "/usr/bin/systemctl",
                    "show",
                    name,
                    "--property=ActiveState,SubState,LoadState,MainPID",
                    "--no-pager",
                ],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout
            props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            result.append(
                {
                    "name": name,
                    "active": props.get("ActiveState", "unknown"),
                    "sub": props.get("SubState", "unknown"),
                    "load": props.get("LoadState", "unknown"),
                }
            )
        except Exception:
            result.append({"name": name, "active": "error", "sub": "error", "load": "error"})
    return result


def collect_docker() -> list[dict[str, Any]]:
    """Direct socket call, no docker CLI required (and avoids group/sudo)."""
    sock_path = "/var/run/docker.sock"
    if not os.path.exists(sock_path):
        return []
    containers: list[dict[str, Any]] = []
    try:
        # `ps`
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(sock_path)
        s.sendall(
            b"GET /containers/json?all=true HTTP/1.0\r\nHost: localhost\r\n\r\n"
        )
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        s.close()
        raw = b"".join(chunks)
        body = raw.split(b"\r\n\r\n", 1)[1]
        data = json.loads(body)
        for c in data:
            containers.append(
                {
                    "id": c.get("Id", "")[:12],
                    "name": (c.get("Names") or ["?"])[0].lstrip("/"),
                    "image": c.get("Image"),
                    "state": c.get("State"),
                    "status": c.get("Status"),
                    "created": c.get("Created"),
                }
            )
    except Exception:
        pass
    return containers


def collect_sysinfo() -> dict[str, Any]:
    """Slow-changing system info shown in the footer block. Refreshes
    only with the rest of the slow_cache (every ~5s)."""
    info: dict[str, Any] = {"host": HOSTNAME, "gpu": "VideoCore VII"}
    # OS / version
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME"):
                    info["os"] = line.strip().split("=", 1)[1].strip('"')
                    break
    except Exception:
        pass
    # Kernel
    try:
        info["kernel"] = subprocess.run(
            ["/usr/bin/uname", "-r"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except Exception:
        pass
    # CPU arch
    try:
        info["arch"] = subprocess.run(
            ["/usr/bin/uname", "-m"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except Exception:
        pass
    # Pi model from /proc/cpuinfo
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Model"):
                    info["model"] = line.split(":", 1)[1].strip()
                    break
    except Exception:
        pass
    # User's login shell from /etc/passwd
    try:
        user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "daniel"
        with open("/etc/passwd") as f:
            for line in f:
                parts = line.split(":")
                if parts[0] == user and len(parts) >= 7:
                    info["shell"] = os.path.basename(parts[6].strip())
                    break
    except Exception:
        pass
    # Installed dpkg package count
    try:
        out = subprocess.run(
            ["/usr/bin/dpkg-query", "-f", ".\n", "-W"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        info["packages"] = sum(1 for _ in out.splitlines())
    except Exception:
        pass
    # Last apt upgrade — last 'upgrade' line in /var/log/dpkg.log
    try:
        last_upgrade = None
        with open("/var/log/dpkg.log") as f:
            for line in f:
                if " upgrade " in line:
                    parts = line.split(maxsplit=2)
                    if len(parts) >= 2:
                        last_upgrade = parts[0] + " " + parts[1]
        info["last_apt"] = last_upgrade
    except Exception:
        pass
    return info


def collect_nvme_smart() -> list[dict[str, Any]]:
    """Per-device temp + wear via smartctl. Quick: ~80ms per device."""
    out: list[dict[str, Any]] = []
    for dev in ("/dev/nvme0n1", "/dev/nvme1n1"):
        if not os.path.exists(dev):
            continue
        try:
            raw = subprocess.run(
                ["/usr/sbin/smartctl", "-A", "-j", dev],
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout
            d = json.loads(raw)
            log = d.get("nvme_smart_health_information_log", {})
            out.append(
                {
                    "device": dev,
                    "temperature_c": log.get("temperature"),
                    "percentage_used": log.get("percentage_used"),
                    "available_spare": log.get("available_spare"),
                    "power_on_hours": log.get("power_on_hours"),
                    "data_units_read": log.get("data_units_read"),
                    "data_units_written": log.get("data_units_written"),
                    "unsafe_shutdowns": log.get("unsafe_shutdowns"),
                    "critical_warning": log.get("critical_warning"),
                }
            )
        except Exception:
            continue
    return out


def collect_disks() -> list[dict[str, Any]]:
    """Filesystem usage. Honest df, not the pironman per-device summary
    (which conflates partitions). We list real mounts only."""
    out: list[dict[str, Any]] = []
    try:
        with open("/proc/mounts") as f:
            mounts = f.readlines()
    except Exception:
        return out
    seen = set()
    interesting = ("/", "/boot/firmware", "/srv")
    for line in mounts:
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fstype = parts[0], parts[1], parts[2]
        if not dev.startswith("/dev/"):
            continue
        if mnt in seen:
            continue
        if fstype in ("tmpfs", "overlay", "devtmpfs", "squashfs"):
            continue
        if not any(mnt == p or mnt.startswith(p + "/") or mnt == p for p in interesting):
            if not mnt.startswith("/srv"):
                continue
        seen.add(mnt)
        try:
            st = os.statvfs(mnt)
            total = st.f_blocks * st.f_frsize
            free = st.f_bfree * st.f_frsize
            avail = st.f_bavail * st.f_frsize
            out.append(
                {
                    "device": dev,
                    "mount": mnt,
                    "fstype": fstype,
                    "total": total,
                    "used": total - free,
                    "available": avail,
                    "percent": ((total - avail) / total * 100) if total else 0,
                }
            )
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


_pub_ip_cache: dict[str, Any] = {"ip": None, "fetched": 0.0}


def collect_public_ip() -> str | None:
    """Public IP via Cloudflare's trace endpoint (Pi-hole-friendly), cached 5 min."""
    now = time.time()
    if _pub_ip_cache["ip"] and (now - _pub_ip_cache["fetched"]) < 300:
        return _pub_ip_cache["ip"]
    try:
        req = urllib.request.Request(
            "https://1.1.1.1/cdn-cgi/trace",
            headers={"User-Agent": "monomi/1.0"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            body = r.read().decode("utf-8")
        for line in body.splitlines():
            if line.startswith("ip="):
                ip = line.split("=", 1)[1].strip()
                if ip:
                    _pub_ip_cache["ip"] = ip
                    _pub_ip_cache["fetched"] = now
                    return ip
    except Exception:
        pass
    return _pub_ip_cache["ip"]


_pub_ipv6_cache: dict[str, Any] = {"ip": None, "fetched": 0.0}


def collect_public_ipv6() -> str | None:
    """Public IPv6 (egress address as seen by Cloudflare). Cached 5 min.
    Returns None if the host has no IPv6 routing."""
    now = time.time()
    if _pub_ipv6_cache["ip"] and (now - _pub_ipv6_cache["fetched"]) < 300:
        return _pub_ipv6_cache["ip"]
    try:
        req = urllib.request.Request(
            "https://[2606:4700:4700::1111]/cdn-cgi/trace",
            headers={"User-Agent": "monomi/1.0"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            body = r.read().decode("utf-8")
        for line in body.splitlines():
            if line.startswith("ip="):
                ip = line.split("=", 1)[1].strip()
                if ip:
                    _pub_ipv6_cache["ip"] = ip
                    _pub_ipv6_cache["fetched"] = now
                    return ip
    except Exception:
        pass
    return _pub_ipv6_cache["ip"]


def collect_netinfo() -> dict[str, Any]:
    """eth0 network details for the net panel footer block: link-level
    info (MTU, MAC), routing (gateway, DNS), IPv6 addresses. The slow
    bits (public IPv6 lookup) are cached separately."""
    info: dict[str, Any] = {}
    # local global-scope IPv6 from /proc/net/if_inet6
    try:
        with open("/proc/net/if_inet6") as f:
            for line in f:
                parts = line.split()
                # cols: addr ifidx prefix_len scope flags devname
                if len(parts) < 6 or parts[5] != "eth0":
                    continue
                # scope 00 = global. Skip 20 (link-local), 10 (site), etc.
                if parts[3] != "00":
                    continue
                raw = parts[0]
                info["ipv6_local"] = ":".join(raw[i:i+4] for i in range(0, 32, 4))
                break
    except Exception:
        pass
    # MTU + MAC
    try:
        with open("/sys/class/net/eth0/mtu") as f:
            info["mtu"] = int(f.read().strip())
    except Exception:
        pass
    try:
        with open("/sys/class/net/eth0/address") as f:
            info["mac"] = f.read().strip()
    except Exception:
        pass
    # default IPv4 gateway from /proc/net/route (hex, little-endian)
    try:
        with open("/proc/net/route") as f:
            f.readline()  # header
            for line in f:
                cols = line.split()
                if len(cols) < 4 or cols[1] != "00000000":
                    continue
                gw = cols[2]
                info["gateway"] = ".".join(
                    str(int(gw[i:i+2], 16)) for i in range(6, -1, -2)
                )
                break
    except Exception:
        pass
    # first nameserver in /etc/resolv.conf
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                if line.startswith("nameserver"):
                    info["dns"] = line.split()[1]
                    break
    except Exception:
        pass
    # public IPv6 (cached 5 min)
    pub6 = _safe(collect_public_ipv6, None)
    if pub6:
        info["ipv6_public"] = pub6
    return info


_wg_pub_ip_cache: dict[str, Any] = {"ip": None, "fetched": 0.0}


def collect_wg_public_ip() -> str | None:
    """Public IP as seen from inside pia-wg's network namespace — i.e.
    the exit IP that remote services see when traffic egresses through
    the wireguard tunnel. We nsenter into the container's netns and
    curl Cloudflare's trace endpoint from there. Cached 5 min."""
    now = time.time()
    if _wg_pub_ip_cache["ip"] and (now - _wg_pub_ip_cache["fetched"]) < 300:
        return _wg_pub_ip_cache["ip"]
    try:
        pid_raw = subprocess.run(
            ["/usr/bin/docker", "inspect", "-f", "{{.State.Pid}}", "pia-wg"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        if not pid_raw or not pid_raw.isdigit():
            return _wg_pub_ip_cache["ip"]
        out = subprocess.run(
            [
                "/usr/bin/nsenter", "-t", pid_raw, "-n",
                "/usr/bin/curl", "-s", "--max-time", "3",
                "-H", "User-Agent: monomi/1.0",
                "https://1.1.1.1/cdn-cgi/trace",
            ],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            if line.startswith("ip="):
                ip = line.split("=", 1)[1].strip()
                if ip:
                    _wg_pub_ip_cache["ip"] = ip
                    _wg_pub_ip_cache["fetched"] = now
                    return ip
    except Exception:
        pass
    return _wg_pub_ip_cache["ip"]


# ── snapshot ─────────────────────────────────────────────────────────────────

# the slow collectors (smartctl, ps, systemctl, docker socket) run on a
# longer cadence so we can ship the fast snapshot every 1 second without
# pegging the Pi.
_slow_cache: dict[str, Any] = {
    "ts": 0.0,
    "data": {
        "services":  [],
        "containers": [],
        "nvme":      [],
        "processes": [],
    },
}
SLOW_INTERVAL = 5.0  # seconds


def _refresh_slow() -> dict[str, Any]:
    now = time.time()
    if (now - _slow_cache["ts"]) < SLOW_INTERVAL and _slow_cache["data"]["nvme"]:
        return _slow_cache["data"]
    _slow_cache["data"] = {
        "services":  _safe(collect_services, []),
        "containers": _safe(collect_docker, []),
        "nvme":      _safe(collect_nvme_smart, []),
        "processes": _safe(collect_processes, []),
        "sysinfo":   _safe(collect_sysinfo, {}),
    }
    _slow_cache["ts"] = now
    return _slow_cache["data"]


def build_snapshot() -> dict[str, Any]:
    slow = _refresh_slow()
    return {
        "host": HOSTNAME,
        "ts": time.time(),
        "interval": INTERVAL,
        "pironman": _safe(collect_pironman, {}),
        "ups":      _safe(collect_ups, {}),
        "throttle": _safe(collect_throttle, {}),
        "uptime":   _safe(collect_uptime, {}),
        "meminfo":  _safe(collect_meminfo, {}),
        "disks":    _safe(collect_disks, []),
        "diskio":   _safe(collect_diskio, {}),
        "iface":    _safe(collect_interface_rates, {}),
        "disk_io":  _safe(collect_disk_io_rates, {}),
        "services": slow["services"],
        "containers": slow["containers"],
        "nvme":     slow["nvme"],
        "processes": slow["processes"],
        "sysinfo":  slow["sysinfo"],
        "netinfo":  _safe(collect_netinfo, {}),
        "public_ip":    _safe(collect_public_ip, None),
        "wg_public_ip": _safe(collect_wg_public_ip, None),
    }


def send(snapshot: dict[str, Any]) -> None:
    body = json.dumps(snapshot).encode("utf-8")
    req = urllib.request.Request(
        INGEST_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {INGEST_TOKEN}",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


def main() -> int:
    print(f"[collector] hostname={HOSTNAME} interval={INTERVAL}s -> {INGEST_URL}", flush=True)
    miss = 0
    while True:
        loop_start = time.time()
        try:
            snap = build_snapshot()
            send(snap)
            miss = 0
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            miss += 1
            if miss <= 3 or miss % 30 == 0:
                print(f"[collector] send failed ({miss}x): {e}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[collector] unexpected: {e}", file=sys.stderr, flush=True)

        elapsed = time.time() - loop_start
        time.sleep(max(0.05, INTERVAL - elapsed))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
