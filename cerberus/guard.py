"""Hardware guard: keeps Cerberus from cooking, starving or filling this computer by accident.

What it does, and (just as important) what it cannot do:

* Heat. Three sources, worst one wins: the graphics card (read straight from the NVIDIA driver's NVML library, with
  `nvidia-smi` as a fallback), the motherboard's thermal zones plus Windows' own "I am throttling for heat" flag (performance
  counters, no admin needed), and, if you run LibreHardwareMonitor with its web server on, the real CPU core temperature.
  Warm = the heads stop cross-checking (lighter load); hot = work pauses until it cools, then stops with a clear message.
  The chips also have their own hardware protection; this only backs off earlier. When nothing can be read, the guard says
  so instead of pretending: it reports "unknown" and relies on the limits below.
* Memory. Low free memory drops the heads to one-at-a-time work and refuses to start big jobs.
* Disk. A write that would leave the drive nearly full is refused.
* Runaway programs. Commands and projects Cerberus runs get a below-normal priority, a memory ceiling, a cap on how many
  processes they may start, and the whole process tree is ended on timeout (Windows job object).
* The guard cannot be switched off. Only its thresholds can be tuned, and they are clamped to safe ranges.

Not covered: the CPU's own die temperature unless LibreHardwareMonitor is running (Windows will not tell a normal program
without a kernel driver), power limits or overclocking (Cerberus has no tool that touches them), and anything you run
yourself outside Cerberus.
"""
from __future__ import annotations

import ctypes
import glob
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import requests

from . import config, hardware
from .safety import Denied

GIB = 1024 ** 3
DEFAULTS = {"gpu_warm": 78, "gpu_hot": 86, "cpu_warm": 85, "cpu_hot": 95, "min_free_gb": 1.5, "min_disk_gb": 5.0,
            "child_mem_gb": 8.0, "child_procs": 64}
LIMITS = {"gpu_hot": (70, 92), "gpu_warm": (60, 88), "cpu_hot": (75, 102), "cpu_warm": (60, 98), "min_free_gb": (1.0, 64.0),
          "min_disk_gb": (1.0, 500.0), "child_mem_gb": (1.0, 64.0), "child_procs": (4, 256)}
INT_KEYS = ("gpu_warm", "gpu_hot", "cpu_warm", "cpu_hot", "child_procs")
COOL_BY = 6              # degrees below "hot" before a sensor counts as cool again (stops flip-flopping)
CACHE_SECONDS = 4.0
FAILED_CACHE_SECONDS = 60.0
LHM_URL = "http://127.0.0.1:8085/data.json"       # LibreHardwareMonitor's web server; fixed to this PC on purpose
LABELS = {"gpu": "graphics card", "cpu": "processor", "board": "motherboard sensor"}
RANK = {"ok": 0, "warm": 1, "hot": 2}

_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}
_level: dict[str, str] = {}


# ---------------------------------------------------------------- settings
def thresholds() -> dict:
    """The tunable limits, clamped so they can never be set to something unsafe (and never to 'off')."""
    raw = (config.load_settings().get("guard") or {})
    out = {}
    for key, default in DEFAULTS.items():
        lo, hi = LIMITS[key]
        try:
            val = float(raw.get(key, default))
        except (TypeError, ValueError):
            val = float(default)
        val = max(lo, min(hi, val))
        out[key] = int(val) if key in INT_KEYS else round(val, 1)
    for kind in ("gpu", "cpu"):
        if out[f"{kind}_warm"] > out[f"{kind}_hot"] - 4:
            out[f"{kind}_warm"] = out[f"{kind}_hot"] - 4
    return out


# ---------------------------------------------------------------- reading the machine
def _num(text: str) -> Optional[float]:
    try:
        return float(text.strip())
    except ValueError:
        return None


class _NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlMem(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


def _nvml_paths() -> list[str]:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    found = [os.path.join(root, "System32", "nvml.dll"),
             os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "NVIDIA Corporation", "NVSMI", "nvml.dll")]
    found += glob.glob(os.path.join(root, "System32", "DriverStore", "FileRepository", "nv_dispi*", "nvml.dll"))
    return [p for p in found if os.path.exists(p)]


def _nvml_read(lib) -> Optional[dict]:
    """Read every NVIDIA card through an already-loaded NVML library and keep the hottest. None when it will not start."""
    if lib.nvmlInit_v2() != 0:
        return None
    try:
        count = ctypes.c_uint()
        if lib.nvmlDeviceGetCount_v2(ctypes.byref(count)) != 0:
            return None
        best: Optional[dict] = None
        for i in range(count.value):
            handle = ctypes.c_void_p()
            temp = ctypes.c_uint()
            if lib.nvmlDeviceGetHandleByIndex_v2(i, ctypes.byref(handle)) != 0 or lib.nvmlDeviceGetTemperature(handle, 0, ctypes.byref(temp)) != 0:
                continue
            card: dict = {"temp_c": float(temp.value), "power_w": None, "power_limit_w": None, "util_pct": None,
                          "vram_used_mb": None, "vram_total_mb": None}
            mw = ctypes.c_uint()
            if lib.nvmlDeviceGetPowerUsage(handle, ctypes.byref(mw)) == 0:
                card["power_w"] = mw.value / 1000
            if lib.nvmlDeviceGetEnforcedPowerLimit(handle, ctypes.byref(mw)) == 0:
                card["power_limit_w"] = mw.value / 1000
            util, mem = _NvmlUtil(), _NvmlMem()
            if lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(util)) == 0:
                card["util_pct"] = float(util.gpu)
            if lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) == 0:
                card["vram_used_mb"], card["vram_total_mb"] = mem.used / 1024 ** 2, mem.total / 1024 ** 2
            if best is None or card["temp_c"] > best["temp_c"]:
                best = card
        return best
    finally:
        lib.nvmlShutdown()


def _probe_nvml() -> Optional[dict]:
    if sys.platform != "win32":
        return None
    for path in _nvml_paths():
        try:
            reading = _nvml_read(ctypes.CDLL(path))
        except (OSError, AttributeError):
            continue
        if reading:
            return reading
    return None


def _probe_gpu() -> Optional[dict]:
    """The card's vitals: straight from the driver's NVML library, then through nvidia-smi. None if both refuse."""
    return _probe_nvml() or _probe_smi()


def _probe_smi() -> Optional[dict]:
    """Ask the NVIDIA tool for the card's vitals. None when it is missing or refuses (it does that on some driver states)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run([exe, "--query-gpu=temperature.gpu,power.draw,power.limit,utilization.gpu,memory.used,memory.total",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=6, creationflags=flags,
                           stdin=subprocess.DEVNULL)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        best: Optional[dict] = None
        for line in r.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 6:
                continue
            temp, power, limit, util, used, total = (_num(p) for p in parts[:6])
            card = {"temp_c": temp, "power_w": power, "power_limit_w": limit, "util_pct": util,
                    "vram_used_mb": used, "vram_total_mb": total}
            if best is None or (card["temp_c"] or 0) > (best["temp_c"] or 0):      # judge by the hottest card
                best = card
        return best
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


class _PdhValue(ctypes.Structure):
    _fields_ = [("status", ctypes.c_ulong), ("value", ctypes.c_longlong)]


class _PdhItem(ctypes.Structure):
    _fields_ = [("name", ctypes.c_wchar_p), ("fmt", _PdhValue)]


def _pdh_values(path: str) -> dict[str, int]:
    """Read one Windows performance counter for every instance (wildcard path). {} on any failure; needs no admin rights."""
    if sys.platform != "win32":
        return {}
    pdh = ctypes.windll.pdh
    query = ctypes.c_void_p()
    if pdh.PdhOpenQueryW(None, ctypes.c_size_t(0), ctypes.byref(query)) != 0:
        return {}
    try:
        counter = ctypes.c_void_p()
        if pdh.PdhAddEnglishCounterW(query, path, ctypes.c_size_t(0), ctypes.byref(counter)) != 0 or pdh.PdhCollectQueryData(query) != 0:
            return {}
        size, count = ctypes.c_ulong(0), ctypes.c_ulong(0)
        fmt_large = 0x400
        if (pdh.PdhGetFormattedCounterArrayW(counter, fmt_large, ctypes.byref(size), ctypes.byref(count), None) & 0xFFFFFFFF) != 0x800007D2:
            return {}
        buf = ctypes.create_string_buffer(size.value)
        if pdh.PdhGetFormattedCounterArrayW(counter, fmt_large, ctypes.byref(size), ctypes.byref(count), buf) != 0:
            return {}
        items = ctypes.cast(buf, ctypes.POINTER(_PdhItem))
        return {items[i].name: items[i].fmt.value for i in range(count.value) if items[i].fmt.status in (0, 1)}
    except (OSError, AttributeError, ValueError):
        return {}
    finally:
        pdh.PdhCloseQuery(query)


def _probe_board() -> Optional[dict]:
    """The motherboard's ACPI thermal zones and Windows' own 'throttling to stay cool' percentage. Not the CPU die itself."""
    tenths_kelvin = _pdh_values(r"\Thermal Zone Information(*)\High Precision Temperature")
    zones = {name: round(v / 10 - 273.15, 1) for name, v in tenths_kelvin.items() if 2000 < v < 5000}
    if not zones:
        return None
    passive = _pdh_values(r"\Thermal Zone Information(*)\% Passive Limit")
    return {"temp_c": max(zones.values()), "zones": zones, "passive_limit_pct": min(passive.values()) if passive else 100}


def _lhm_cpu_temp(tree: dict) -> Optional[float]:
    """Hottest CPU temperature in LibreHardwareMonitor's data.json (ignores 'distance to TjMax', which is also in degrees)."""
    temps: list[float] = []

    def walk(node: dict, in_cpu: bool) -> None:
        in_cpu = in_cpu or "cpu" in str(node.get("ImageURL", "")).lower()
        value, text = str(node.get("Value", "")), str(node.get("Text", "")).lower()
        if in_cpu and "°C" in value and "distance" not in text and "tjmax" not in text:
            m = re.search(r"-?\d+(?:[.,]\d+)?", value)
            if m:
                temps.append(float(m.group().replace(",", ".")))
        for child in node.get("Children") or []:
            if isinstance(child, dict):
                walk(child, in_cpu)

    walk(tree, False)
    return max(temps) if temps else None


def _probe_cpu() -> Optional[dict]:
    """The real CPU temperature, if LibreHardwareMonitor is running on this PC with its web server on. Otherwise None."""
    try:
        r = requests.get(LHM_URL, timeout=1.5)
        temp = _lhm_cpu_temp(r.json()) if r.ok else None
    except (requests.RequestException, ValueError, AttributeError):
        return None
    return {"temp_c": temp, "source": "LibreHardwareMonitor"} if temp is not None else None


def _cached(name: str, probe: Callable[[], Optional[dict]], force: bool) -> Optional[dict]:
    """Latest reading (kept a few seconds; a failing probe is not retried every moment)."""
    with _lock:
        entry = _cache.get(name)
        if entry and not force and time.monotonic() - entry[0] < (CACHE_SECONDS if entry[1] else FAILED_CACHE_SECONDS):
            return entry[1]
    reading = probe()
    with _lock:
        _cache[name] = (time.monotonic(), reading)
    return reading


def gpu(force: bool = False) -> Optional[dict]:
    return _cached("gpu", _probe_gpu, force)


def cpu(force: bool = False) -> Optional[dict]:
    return _cached("cpu", _probe_cpu, force)


def board(force: bool = False) -> Optional[dict]:
    return _cached("board", _probe_board, force)


def reset_cache() -> None:
    with _lock:
        _cache.clear()
        _level.clear()


def _classify(key: str, temp: float, warm: float, hot: float) -> str:
    """ok | warm | hot, with hysteresis so a sensor hovering at the line does not flip every few seconds."""
    prev = _level.get(key, "ok")
    if temp >= hot or (prev == "hot" and temp > hot - COOL_BY):
        level = "hot"
    elif temp >= warm or (prev == "warm" and temp > warm - 3) or (prev == "hot"):
        level = "warm"
    else:
        level = "ok"
    _level[key] = level
    return level


def disk_free_gb(path: Optional[Path | str] = None) -> Optional[float]:
    try:
        p = Path(path) if path else config.DATA_DIR
        while not p.exists() and p.parent != p:
            p = p.parent
        return round(shutil.disk_usage(str(p)).free / GIB, 1)
    except OSError:
        return None


def status(force: bool = False) -> dict:
    """Everything the UI and the orchestrator need to know, in one place."""
    th = thresholds()
    card, proc, mobo = gpu(force), cpu(force), board(force)
    readings: dict[str, float] = {}
    levels: dict[str, str] = {}
    if card and card.get("temp_c") is not None:
        readings["gpu"] = card["temp_c"]
        levels["gpu"] = _classify("gpu", card["temp_c"], th["gpu_warm"], th["gpu_hot"])
    if proc:
        readings["cpu"] = proc["temp_c"]
        levels["cpu"] = _classify("cpu", proc["temp_c"], th["cpu_warm"], th["cpu_hot"])
    forced_warm = False
    if mobo:
        readings["board"] = mobo["temp_c"]
        levels["board"] = _classify("board", mobo["temp_c"], th["cpu_warm"], th["cpu_hot"])
        if mobo["passive_limit_pct"] < 100 and levels["board"] == "ok":
            levels["board"], forced_warm = "warm", True      # Windows itself is slowing the machine down to stay cool
    level = max(levels.values(), key=RANK.__getitem__) if levels else "unknown"
    hottest = None
    if levels:
        key = max(levels, key=lambda k: (RANK[levels[k]], readings[k]))
        hottest = {"key": key, "label": LABELS[key], "temp_c": readings[key], "level": levels[key]}
    mem = hardware.pressure()
    free_gb = mem.get("free_gb", 0.0)
    mem_low = bool(mem.get("low")) or (0 < free_gb < th["min_free_gb"])
    disk = disk_free_gb()
    disk_low = disk is not None and disk < th["min_disk_gb"]
    notes: list[str] = []
    if not levels:
        notes.append("No temperature can be read right now, so heat is NOT being watched. Memory, disk and runaway-program "
                     "limits are still enforced.")
    else:
        if card is None:
            notes.append("The graphics card's temperature cannot be read right now.")
        if proc is None:
            notes.append("The processor's own temperature is not available to Windows programs. Run LibreHardwareMonitor "
                         "with its web server on (port 8085) to add it" + ("; the motherboard sensors are watched meanwhile." if mobo else "."))
    if hottest and hottest["level"] in ("warm", "hot") and not (forced_warm and hottest["key"] == "board"):
        word, name, t = hottest["level"], hottest["label"], hottest["temp_c"]
        notes.append(f"The {name} is {word} ({t:g} C). " + ("Work is paused until it cools." if word == "hot"
                                                          else "The heads work one at a time without cross-checking."))
    if mobo and mobo["passive_limit_pct"] < 100:
        notes.append(f"Windows is slowing the processor to {mobo['passive_limit_pct']:g}% to keep cool.")
    if mem_low:
        notes.append(f"Only {free_gb:g} GB of memory is free. Heavy work is held back.")
    if disk_low:
        notes.append(f"Only {disk:g} GB of disk space is free. Writing files is refused.")
    return {"gpu": card, "cpu": proc, "board": mobo, "hottest": hottest, "temp_known": bool(levels), "level": level,
            "memory_free_gb": free_gb, "memory_low": mem_low, "disk_free_gb": disk, "disk_low": disk_low,
            "throttle": level in ("warm", "hot") or mem_low, "blocked": level == "hot", "thresholds": th, "notes": notes}


def should_throttle() -> bool:
    try:
        return bool(status()["throttle"])
    except Exception:
        return False


def ensure_safe(cancel: Optional[threading.Event] = None, max_wait: float = 150.0, *,
                sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> tuple[bool, str]:
    """Wait (up to max_wait seconds) for a hot machine to cool. Returns (ok, message). ok=False means: still too hot, do not run."""
    start = clock()
    st = status(force=True)
    while st["blocked"]:
        if cancel is not None and cancel.is_set():
            return False, "Cancelled while waiting for the computer to cool."
        if clock() - start >= max_wait:
            hot = st["hottest"]
            return False, (f"The {hot['label']} is still too hot ({hot['temp_c']:g} C). "
                           "Cerberus stopped to protect it; try again in a few minutes.")
        sleep(5.0)
        st = status(force=True)
    return True, ""


def assert_space(path: Path | str, nbytes: int = 0) -> None:
    """Refuse a write that would leave the drive nearly full."""
    free = disk_free_gb(path)
    if free is None:
        return
    th = thresholds()
    if free - nbytes / GIB < th["min_disk_gb"]:
        raise Denied(f"Only {free:g} GB is free on that drive and the safety limit is {th['min_disk_gb']:g} GB, so nothing "
                     "more is written there.")


# ---------------------------------------------------------------- running other programs safely
BELOW_NORMAL_PRIORITY = 0x00004000
CREATE_NO_WINDOW = 0x08000000


class _Job:
    """A Windows job object: memory ceiling, process-count cap, and 'end everything inside it' with one call."""

    def __init__(self, mem_bytes: int, max_procs: int) -> None:
        from ctypes import wintypes
        self.h = None
        self._k = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self._k
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.CloseHandle.argtypes = [wintypes.HANDLE]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        handle = k.CreateJobObjectW(None, None)
        if not handle:
            return
        info = EXT()
        info.Basic.LimitFlags = 0x2000 | 0x100 | 0x8          # kill-on-close | per-process memory | active-process limit
        info.Basic.ActiveProcessLimit = int(max_procs)
        info.ProcessMemoryLimit = int(mem_bytes)
        if not k.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k.CloseHandle(handle)
            return
        self.h = handle

    def assign(self, proc_handle) -> bool:
        return bool(self.h) and bool(self._k.AssignProcessToJobObject(self.h, int(proc_handle)))

    def kill_all(self) -> None:
        if self.h:
            self._k.TerminateJobObject(self.h, 1)

    def close(self) -> None:
        if self.h:
            self._k.CloseHandle(self.h)
            self.h = None


def child_run(argv: list[str], cwd: str, timeout: float, *, mem_gb: Optional[float] = None, procs: Optional[int] = None) -> dict:
    """Run a program with its output captured, inside the safety limits. Returns {code, out, err, timed_out, limited}.
    `limited` is False when the job-object limits could not be applied (the run still has below-normal priority and a timeout)."""
    th = thresholds()
    mem = int((mem_gb if mem_gb is not None else th["child_mem_gb"]) * GIB)
    flags = (BELOW_NORMAL_PRIORITY | CREATE_NO_WINDOW) if sys.platform == "win32" else 0
    kwargs: dict = {"cwd": cwd, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True, "errors": "replace",
                    "stdin": subprocess.DEVNULL}
    if flags:
        kwargs["creationflags"] = flags
    proc = subprocess.Popen(argv, **kwargs)
    job: Optional[_Job] = None
    limited = False
    if sys.platform == "win32":
        try:
            job = _Job(mem, procs if procs is not None else th["child_procs"])
            limited = job.assign(proc._handle)                                # type: ignore[attr-defined]
        except (OSError, AttributeError):
            job, limited = None, False
    timed_out = False
    try:
        out, err = proc.communicate(timeout=max(1.0, float(timeout)))
    except subprocess.TimeoutExpired:
        timed_out = True
        if job and job.h:
            job.kill_all()
        proc.kill()
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:                                  # a leftover child is still holding the pipes
            out, err = "", ""
    finally:
        if job:
            job.close()                                                       # kill-on-close ends any stragglers
    return {"code": proc.returncode, "out": out or "", "err": err or "", "timed_out": timed_out, "limited": limited}
