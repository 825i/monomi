#!/usr/bin/env python3
"""
monomi — your watchtower, always watching.

One Python process does the lot:
  - collector thread that gathers a /proc + /sys snapshot every
    INTERVAL seconds
  - threading HTTP server on BIND_ADDR:PORT (defaults to 127.0.0.1:8080)
  - thread-safe state object holding the latest snapshot plus a rolling
    history (~6 minutes worth)

The dashboard is served straight off the local box: stick a Cloudflare
Tunnel, nginx, or just plain LAN access in front of localhost:8080 and
you're done. No remote backend, no edge worker, no request quotas.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# How often the collector samples /proc + /sys etc. Lower = livelier
# updates but more host work. 0.5s feels properly real-time on a Pi 5;
# bump higher if you're running on something smaller.
INTERVAL = float(os.environ.get("INTERVAL", "0.5"))

# Where the HTTP server listens. localhost is correct when a tunnel
# fronts the service. Set BIND_ADDR=0.0.0.0 to expose on the LAN.
BIND_ADDR = os.environ.get("BIND_ADDR", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))

# How many history points to retain. At 2s cadence, 180 ≈ 6 minutes;
# the dashboard's braille graphs read the most recent N for rendering.
HISTORY_CAP = int(os.environ.get("HISTORY_CAP", "360"))

# Optional integrations
PIRONMAN_URL = os.environ.get(
    "PIRONMAN_URL", "http://127.0.0.1:34001/api/v1.0/get-data"
)
NUT_UPS = os.environ.get("NUT_UPS", "eaton3s")

HOSTNAME = socket.gethostname()

# Where the static dashboard assets live, relative to this file. The
# installer drops monomi.py and the assets/ directory side by side
# under /opt/monomi/.
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

WATCHED_SERVICES = [
    "ssh", "docker", "nginx", "smbd", "openmediavault-engined",
    "pironman5", "fail2ban", "nut-server", "nut-monitor",
    "chrony", "monit", "influxdb",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: float = 3.0) -> str:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=True,
    ).stdout


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Collectors — same set as the original pi5-status collector
# ---------------------------------------------------------------------------


def collect_pironman() -> dict[str, Any]:
    with urllib.request.urlopen(PIRONMAN_URL, timeout=2) as r:
        payload = json.load(r)
    return payload.get("data", {}) if payload.get("status") else {}


def collect_ups() -> dict[str, Any]:
    out = _run(["/usr/bin/upsc", NUT_UPS])
    result: dict[str, Any] = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        try:
            result[k.strip()] = float(v) if "." in v else int(v)
        except ValueError:
            result[k.strip()] = v
    return result


def collect_throttle() -> dict[str, Any]:
    throttled_raw = _run(["/usr/bin/vcgencmd", "get_throttled"]).strip()
    volt_raw = _run(["/usr/bin/vcgencmd", "measure_volts"]).strip()
    value = int(throttled_raw.split("=")[1], 16) if "=" in throttled_raw else 0
    return {
        "raw": throttled_raw,
        "value": value,
        "currently_throttled":     bool(value & 0x4),
        "under_voltage_now":       bool(value & 0x1),
        "freq_capped_now":         bool(value & 0x2),
        "soft_temp_limit_now":     bool(value & 0x8),
        "throttled_since_boot":    bool(value & 0x40000),
        "under_voltage_since_boot":bool(value & 0x10000),
        "voltage": volt_raw.split("=")[1] if "=" in volt_raw else volt_raw,
    }


def collect_uptime() -> dict[str, Any]:
    with open("/proc/uptime") as f:
        up = float(f.read().split()[0])
    with open("/proc/loadavg") as f:
        parts = f.read().split()
    return {
        "uptime_seconds": up,
        "load_1":  float(parts[0]),
        "load_5":  float(parts[1]),
        "load_15": float(parts[2]),
    }


def collect_meminfo() -> dict[str, Any]:
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
                    out[k.strip()] = int(parts[0]) * 1024
                except ValueError:
                    continue
    except Exception:
        return {}
    total   = out.get("MemTotal", 0)
    free    = out.get("MemFree", 0)
    avail   = out.get("MemAvailable", 0)
    cached  = out.get("Cached", 0) + out.get("SReclaimable", 0)
    buffers = out.get("Buffers", 0)
    used    = max(0, total - free - cached - buffers)
    sw_total = out.get("SwapTotal", 0)
    sw_free  = out.get("SwapFree", 0)
    sw_used  = max(0, sw_total - sw_free)
    return {
        "total": total, "used": used, "free": free, "available": avail,
        "cached": cached, "buffers": buffers,
        "swap_total": sw_total, "swap_used": sw_used, "swap_free": sw_free,
    }


def collect_processes(limit: int = 40) -> list[dict[str, Any]]:
    try:
        out = subprocess.run(
            ["/usr/bin/ps", "axo", "pid,ppid,user:16,pcpu,pmem,rss,nlwp,comm,args",
             "--sort=-pcpu", "--no-headers"],
            capture_output=True, text=True, timeout=2,
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
                "pid": int(parts[0]), "ppid": int(parts[1]), "user": parts[2],
                "cpu": float(parts[3]), "mem": float(parts[4]),
                "rss_kb": int(parts[5]), "threads": int(parts[6]),
                "program": parts[7], "cmd": parts[8],
            })
        except ValueError:
            continue
        if len(rows) >= limit:
            break
    return rows


# previous-sample cache for interface rate calc
_iface_last: dict[str, dict[str, float]] = {}


def _read_proc_net_dev(path: str) -> dict[str, tuple[int, int]]:
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


def _delta_rate(iface: str, rx: int, tx: int, now: float):
    prev = _iface_last.get(iface)
    _iface_last[iface] = {"rx": rx, "tx": tx, "ts": now}
    if not prev:
        return None, None
    dt = now - prev["ts"]
    if dt <= 0:
        return None, None
    return max(0, rx - prev["rx"]) / dt, max(0, tx - prev["tx"]) / dt


def collect_interface_rates() -> dict[str, dict[str, float | None]]:
    """eth0 from /proc/net/dev, wg0 from inside pia-wg's net namespace."""
    now = time.time()
    rates: dict[str, dict[str, float | None]] = {}
    host = _read_proc_net_dev("/proc/net/dev")
    if "eth0" in host:
        rx, tx = host["eth0"]
        d, u = _delta_rate("eth0", rx, tx, now)
        rates["eth0"] = {"down_Bps": d, "up_Bps": u, "rx_bytes": rx, "tx_bytes": tx}
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


# Pool device list — see README for how to point this at your own disks
POOL_DEVICES = ("nvme0n1p3", "nvme1n1")
_disk_io_last: dict[str, float] = {}


def collect_disk_io_rates() -> dict[str, float | None]:
    now = time.time()
    total_r = total_w = 0
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 14:
                    continue
                if parts[2] not in POOL_DEVICES:
                    continue
                total_r += int(parts[5]) * 512
                total_w += int(parts[9]) * 512
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
                if not (name.startswith("nvme") or name.startswith("sd") or name.startswith("mmcblk")):
                    continue
                out[name] = {
                    "read_bytes":  int(parts[5]) * 512,
                    "write_bytes": int(parts[9]) * 512,
                }
    except Exception:
        pass
    return out


def collect_services() -> list[dict[str, Any]]:
    result = []
    for name in WATCHED_SERVICES:
        try:
            out = subprocess.run(
                ["/usr/bin/systemctl", "show", name,
                 "--property=ActiveState,SubState,LoadState,MainPID",
                 "--no-pager"],
                capture_output=True, text=True, timeout=2,
            ).stdout
            props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            result.append({
                "name": name,
                "active": props.get("ActiveState", "unknown"),
                "sub":    props.get("SubState", "unknown"),
                "load":   props.get("LoadState", "unknown"),
            })
        except Exception:
            result.append({"name": name, "active": "error", "sub": "error", "load": "error"})
    return result


def collect_docker() -> list[dict[str, Any]]:
    sock_path = "/var/run/docker.sock"
    if not os.path.exists(sock_path):
        return []
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(sock_path)
        s.sendall(b"GET /containers/json?all=true HTTP/1.0\r\nHost: localhost\r\n\r\n")
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        s.close()
        body = b"".join(chunks).split(b"\r\n\r\n", 1)[1]
        data = json.loads(body)
        return [
            {"id": c.get("Id", "")[:12],
             "name": (c.get("Names") or ["?"])[0].lstrip("/"),
             "image": c.get("Image"), "state": c.get("State"),
             "status": c.get("Status"), "created": c.get("Created")}
            for c in data
        ]
    except Exception:
        return []


def collect_nvme_smart() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for dev in ("/dev/nvme0n1", "/dev/nvme1n1"):
        if not os.path.exists(dev):
            continue
        try:
            raw = subprocess.run(
                ["/usr/sbin/smartctl", "-A", "-j", dev],
                capture_output=True, text=True, timeout=3,
            ).stdout
            d = json.loads(raw)
            log = d.get("nvme_smart_health_information_log", {})
            out.append({
                "device": dev,
                "temperature_c":      log.get("temperature"),
                "percentage_used":    log.get("percentage_used"),
                "available_spare":    log.get("available_spare"),
                "power_on_hours":     log.get("power_on_hours"),
                "data_units_read":    log.get("data_units_read"),
                "data_units_written": log.get("data_units_written"),
                "unsafe_shutdowns":   log.get("unsafe_shutdowns"),
                "critical_warning":   log.get("critical_warning"),
            })
        except Exception:
            continue
    return out


def collect_disks() -> list[dict[str, Any]]:
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
        if not any(mnt == p or mnt.startswith(p + "/") for p in interesting):
            if not mnt.startswith("/srv"):
                continue
        seen.add(mnt)
        try:
            st = os.statvfs(mnt)
            total = st.f_blocks * st.f_frsize
            free  = st.f_bfree  * st.f_frsize
            avail = st.f_bavail * st.f_frsize
            out.append({
                "device": dev, "mount": mnt, "fstype": fstype,
                "total": total, "used": total - free, "available": avail,
                "percent": ((total - avail) / total * 100) if total else 0,
            })
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Sysinfo + net details + public IP lookups (slow-cached)
# ---------------------------------------------------------------------------


def collect_sysinfo() -> dict[str, Any]:
    info: dict[str, Any] = {"host": HOSTNAME, "gpu": "VideoCore VII"}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME"):
                    info["os"] = line.strip().split("=", 1)[1].strip('"')
                    break
    except Exception:
        pass
    try:
        info["kernel"] = subprocess.run(
            ["/usr/bin/uname", "-r"], capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except Exception:
        pass
    try:
        info["arch"] = subprocess.run(
            ["/usr/bin/uname", "-m"], capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except Exception:
        pass
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Model"):
                    info["model"] = line.split(":", 1)[1].strip()
                    break
    except Exception:
        pass
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
    try:
        out = subprocess.run(
            ["/usr/bin/dpkg-query", "-f", ".\n", "-W"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        info["packages"] = sum(1 for _ in out.splitlines())
    except Exception:
        pass
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


_pub_ip_cache:    dict[str, Any] = {"ip": None, "fetched": 0.0}
_pub_ipv6_cache:  dict[str, Any] = {"ip": None, "fetched": 0.0}
_wg_pub_ip_cache: dict[str, Any] = {"ip": None, "fetched": 0.0}


def _trace_ip(host: str, cache: dict[str, Any]) -> str | None:
    now = time.time()
    if cache["ip"] and (now - cache["fetched"]) < 300:
        return cache["ip"]
    try:
        req = urllib.request.Request(host, headers={"User-Agent": "monomi/1.0"})
        with urllib.request.urlopen(req, timeout=3) as r:
            body = r.read().decode("utf-8")
        for line in body.splitlines():
            if line.startswith("ip="):
                ip = line.split("=", 1)[1].strip()
                if ip:
                    cache["ip"] = ip
                    cache["fetched"] = now
                    return ip
    except Exception:
        pass
    return cache["ip"]


def collect_public_ip() -> str | None:
    return _trace_ip("https://1.1.1.1/cdn-cgi/trace", _pub_ip_cache)


def collect_public_ipv6() -> str | None:
    return _trace_ip("https://[2606:4700:4700::1111]/cdn-cgi/trace", _pub_ipv6_cache)


def collect_wg_public_ip() -> str | None:
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
            ["/usr/bin/nsenter", "-t", pid_raw, "-n",
             "/usr/bin/curl", "-s", "--max-time", "3",
             "-H", "User-Agent: monomi/1.0",
             "https://1.1.1.1/cdn-cgi/trace"],
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


def collect_netinfo() -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        with open("/proc/net/if_inet6") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 6 or parts[5] != "eth0":
                    continue
                if parts[3] != "00":   # 00 = global scope
                    continue
                raw = parts[0]
                info["ipv6_local"] = ":".join(raw[i:i+4] for i in range(0, 32, 4))
                break
    except Exception:
        pass
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
    try:
        with open("/proc/net/route") as f:
            f.readline()
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
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                if line.startswith("nameserver"):
                    info["dns"] = line.split()[1]
                    break
    except Exception:
        pass
    pub6 = _safe(collect_public_ipv6, None)
    if pub6:
        info["ipv6_public"] = pub6
    return info


# ---------------------------------------------------------------------------
# Snapshot orchestration — slow paths cached on a longer cadence so the
# fast snapshot stays cheap.
# ---------------------------------------------------------------------------

_slow_cache: dict[str, Any] = {
    "ts": 0.0,
    "data": {"services": [], "containers": [], "nvme": [], "processes": [], "sysinfo": {}},
}
SLOW_INTERVAL = 5.0


def _refresh_slow() -> dict[str, Any]:
    now = time.time()
    if (now - _slow_cache["ts"]) < SLOW_INTERVAL and _slow_cache["data"]["sysinfo"]:
        return _slow_cache["data"]
    _slow_cache["data"] = {
        "services":   _safe(collect_services, []),
        "containers": _safe(collect_docker, []),
        "nvme":       _safe(collect_nvme_smart, []),
        "processes":  _safe(collect_processes, []),
        "sysinfo":    _safe(collect_sysinfo, {}),
    }
    _slow_cache["ts"] = now
    return _slow_cache["data"]


def build_snapshot() -> dict[str, Any]:
    slow = _refresh_slow()
    return {
        "host":     HOSTNAME,
        "ts":       time.time(),
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
        "services":   slow["services"],
        "containers": slow["containers"],
        "nvme":       slow["nvme"],
        "processes":  slow["processes"],
        "sysinfo":    slow["sysinfo"],
        "netinfo":    _safe(collect_netinfo, {}),
        "public_ip":     _safe(collect_public_ip, None),
        "wg_public_ip":  _safe(collect_wg_public_ip, None),
    }


# ---------------------------------------------------------------------------
# Shared state — collector writes, HTTP handler reads. Thread-safe.
# ---------------------------------------------------------------------------

HISTORY_FIELDS = [
    "ts", "cpu_percent",
    "cpu_0_percent", "cpu_1_percent", "cpu_2_percent", "cpu_3_percent",
    "cpu_temperature",
    "memory_percent", "swap_percent",
    "network_download_speed", "network_upload_speed",
    "eth0_down", "eth0_up",
    "wg0_down", "wg0_up",
    "disk_read", "disk_write",
]


def _num(v):
    return v if isinstance(v, (int, float)) and v == v else None


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.latest: dict[str, Any] | None = None
        self.history: dict[str, list] = {k: [] for k in HISTORY_FIELDS}

    def update(self, snap: dict[str, Any]) -> None:
        with self.lock:
            self.latest = snap
            self._push(snap)

    def get(self) -> dict[str, Any]:
        with self.lock:
            return {"latest": self.latest, "history": {k: list(v) for k, v in self.history.items()}}

    def _push(self, snap: dict[str, Any]) -> None:
        pm    = snap.get("pironman") or {}
        mi    = snap.get("meminfo") or {}
        iface = snap.get("iface") or {}
        eth0  = iface.get("eth0") or {}
        wg0   = iface.get("wg0")  or {}
        dio   = snap.get("disk_io") or {}

        swap_pct = None
        sw_total = _num(mi.get("swap_total"))
        sw_used  = _num(mi.get("swap_used"))
        if sw_total and sw_total > 0 and sw_used is not None:
            swap_pct = (sw_used / sw_total) * 100

        h = self.history
        h["ts"].append(snap.get("ts") if isinstance(snap.get("ts"), (int, float)) else time.time())
        h["cpu_percent"].append(_num(pm.get("cpu_percent")))
        h["cpu_0_percent"].append(_num(pm.get("cpu_0_percent")))
        h["cpu_1_percent"].append(_num(pm.get("cpu_1_percent")))
        h["cpu_2_percent"].append(_num(pm.get("cpu_2_percent")))
        h["cpu_3_percent"].append(_num(pm.get("cpu_3_percent")))
        h["cpu_temperature"].append(_num(pm.get("cpu_temperature")))
        h["memory_percent"].append(_num(pm.get("memory_percent")))
        h["swap_percent"].append(swap_pct)
        h["network_download_speed"].append(_num(pm.get("network_download_speed")))
        h["network_upload_speed"].append(_num(pm.get("network_upload_speed")))
        h["eth0_down"].append(_num(eth0.get("down_Bps")))
        h["eth0_up"].append(_num(eth0.get("up_Bps")))
        h["wg0_down"].append(_num(wg0.get("down_Bps")))
        h["wg0_up"].append(_num(wg0.get("up_Bps")))
        h["disk_read"].append(_num(dio.get("read_Bps")))
        h["disk_write"].append(_num(dio.get("write_Bps")))

        for k in h:
            while len(h[k]) > HISTORY_CAP:
                h[k].pop(0)


STATE = State()


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

_ASSET_CACHE: dict[str, tuple[bytes, str]] = {}
ASSET_MAP = {
    "/":            ("index.html", "text/html; charset=utf-8"),
    "/index.html":  ("index.html", "text/html; charset=utf-8"),
    "/app.js":      ("app.js",     "application/javascript; charset=utf-8"),
    "/style.css":   ("style.css",  "text/css; charset=utf-8"),
}


def _load_asset(name: str, ctype: str) -> tuple[bytes, str]:
    """Read once, cache forever — assets don't change between deploys."""
    if name in _ASSET_CACHE:
        return _ASSET_CACHE[name]
    body = (ASSETS_DIR / name).read_bytes()
    _ASSET_CACHE[name] = (body, ctype)
    return body, ctype


class Handler(BaseHTTPRequestHandler):
    # Quiet the default per-request stderr noise; systemd journal already
    # captures whatever we print explicitly.
    def log_message(self, *args) -> None:
        pass

    def _send(self, status: int, body: bytes, ctype: str, no_store: bool = True) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/stats":
            body = json.dumps(STATE.get()).encode("utf-8")
            return self._send(200, body, "application/json; charset=utf-8")
        if path in ASSET_MAP:
            try:
                body, ctype = _load_asset(*ASSET_MAP[path])
            except FileNotFoundError:
                return self._send(404, b"not found", "text/plain; charset=utf-8")
            return self._send(200, body, ctype)
        self._send(404, b"not found", "text/plain; charset=utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def collector_loop() -> None:
    """Background thread: refresh STATE every INTERVAL seconds."""
    miss = 0
    while True:
        loop_start = time.time()
        try:
            STATE.update(build_snapshot())
            miss = 0
        except Exception as e:
            miss += 1
            if miss <= 3 or miss % 30 == 0:
                print(f"[collector] snapshot failed ({miss}x): {e}", file=sys.stderr, flush=True)
        elapsed = time.time() - loop_start
        time.sleep(max(0.05, INTERVAL - elapsed))


def main() -> int:
    print(
        f"[monomi] host={HOSTNAME} listening on {BIND_ADDR}:{PORT} "
        f"interval={INTERVAL}s history_cap={HISTORY_CAP} "
        f"assets={ASSETS_DIR}",
        flush=True,
    )
    threading.Thread(target=collector_loop, daemon=True).start()
    with ThreadingHTTPServer((BIND_ADDR, PORT), Handler) as httpd:
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
