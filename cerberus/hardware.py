"""Reads this computer's specs and decides what Cerberus can comfortably handle.

Nothing here needs the internet or admin rights. Every probe can fail (drivers, odd Windows builds) and then falls
back to something safe, so detection never stops the app from opening.
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

GIB = 1024 ** 3

# Used only when Windows and the driver will not tell us the card's memory (WMI caps it at 4 GB for big cards).
GPU_VRAM_TABLE_GB = [
    (r"rtx\s*5090", 32), (r"rtx\s*5080", 16), (r"rtx\s*5070\s*ti", 16), (r"rtx\s*5070", 12), (r"rtx\s*5060\s*ti", 16),
    (r"rtx\s*5060", 8), (r"rtx\s*4090", 24), (r"rtx\s*4080", 16), (r"rtx\s*4070\s*ti\s*super", 16), (r"rtx\s*4070\s*ti", 12),
    (r"rtx\s*4070", 12), (r"rtx\s*4060\s*ti", 8), (r"rtx\s*4060", 8), (r"rtx\s*3090", 24), (r"rtx\s*3080\s*ti", 12),
    (r"rtx\s*3080", 10), (r"rtx\s*3070", 8), (r"rtx\s*3060\s*ti", 8), (r"rtx\s*3060", 12), (r"rtx\s*2080", 8),
    (r"rtx\s*2070", 8), (r"rtx\s*2060", 6), (r"gtx\s*1660", 6), (r"gtx\s*1650", 4), (r"rx\s*7900\s*xtx", 24),
    (r"rx\s*7900", 20), (r"rx\s*7800", 16), (r"rx\s*7700", 12), (r"rx\s*6800", 16),
]


# ---------------------------------------------------------------- probes
class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]


def memory() -> tuple[int, int]:
    """(total, available) physical memory in bytes. (0, 0) if it cannot be read."""
    try:
        if sys.platform == "win32":
            s = _MemStatus()
            s.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
                return int(s.ullTotalPhys), int(s.ullAvailPhys)
        else:
            page = os.sysconf("SC_PAGE_SIZE")
            return os.sysconf("SC_PHYS_PAGES") * page, os.sysconf("SC_AVPHYS_PAGES") * page
    except (OSError, ValueError, AttributeError):
        pass
    return 0, 0


def _cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    import platform
    return platform.processor() or "unknown CPU"


def _gpus_from_registry() -> list[dict]:
    """Display adapters and their real memory (the driver stores it as a 64-bit number, unlike WMI's capped 32-bit one)."""
    out: list[dict] = []
    if sys.platform != "win32":
        return out
    try:
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as k:
                        name = str(winreg.QueryValueEx(k, "DriverDesc")[0])
                        try:
                            mem = int(winreg.QueryValueEx(k, "HardwareInformation.qwMemorySize")[0])
                        except (OSError, ValueError, TypeError):
                            mem = 0
                    out.append({"name": name, "vram": mem})
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _gpus_from_nvidia_smi() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        flags = 0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        r = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=6, creationflags=flags)
        if r.returncode != 0:
            return []
        gpus = []
        for line in r.stdout.splitlines():
            name, _, mb = line.rpartition(",")
            if name.strip() and mb.strip().isdigit():
                gpus.append({"name": name.strip(), "vram": int(mb.strip()) * 1024 * 1024})
        return gpus
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def vram_from_name(name: str) -> int:
    low = name.lower()
    for pattern, gb in GPU_VRAM_TABLE_GB:
        if re.search(pattern, low):
            return gb * GIB
    return 0


def _is_discrete(name: str) -> bool:
    return bool(re.search(r"nvidia|geforce|rtx|gtx|radeon rx|radeon pro|arc\s*[ab]\d", name, re.I))


def gpus() -> list[dict]:
    found = _gpus_from_nvidia_smi() or _gpus_from_registry()
    for g in found:
        if not g["vram"] or (g["vram"] <= 4 * GIB and _is_discrete(g["name"])):   # 4 GB = the old 32-bit cap, so not trustworthy
            g["vram"] = vram_from_name(g["name"]) or g["vram"]
    return [g for g in found if g["name"] and "basic" not in g["name"].lower()]


# ---------------------------------------------------------------- the report
def detect(data_dir: Optional[Path] = None) -> dict:
    total, avail = memory()
    cards = gpus()
    best = max(cards, key=lambda g: g["vram"], default=None)
    try:
        disk = shutil.disk_usage(str(data_dir) if data_dir and Path(data_dir).exists() else str(Path.home()))
        disk_free = disk.free
    except OSError:
        disk_free = 0
    return {
        "cpu": _cpu_name(), "cores": os.cpu_count() or 1,
        "ram_gb": round(total / GIB, 1), "ram_free_gb": round(avail / GIB, 1),
        "gpu": best["name"] if best else "", "vram_gb": round((best["vram"] if best else 0) / GIB, 1),
        "gpus": [g["name"] for g in cards], "disk_free_gb": round(disk_free / GIB),
    }


def tier(hw: dict) -> str:
    """strong | good | light, judged mostly on the graphics card because that is what runs the models."""
    vram, ram = hw.get("vram_gb", 0), hw.get("ram_gb", 0)
    if vram >= 12 and ram >= 24:
        return "strong"
    if vram >= 6 or ram >= 16:
        return "good"
    return "light"


# Bigger/smarter models worth pulling for each tier (never downloaded automatically: they are several GB each).
UPGRADES = {
    "strong": [("deepseek-r1:14b", "A deeper thinker for the reasoning head (~9 GB)"),
               ("qwen2.5:14b", "A sharper all-round head (~9 GB)"),
               ("gemma3:12b", "A strong, careful second opinion (~8 GB)")],
    "good": [("qwen2.5:7b", "A sharper all-round head (~5 GB)"), ("gemma3:4b", "A small, quick second opinion (~3 GB)")],
    "light": [("llama3.2:3b", "A small head that fits modest computers (~2 GB)")],
}


def recommend(hw: dict) -> dict:
    """Settings that suit this machine. Context is the main lever: bigger = remembers more of a long task, uses more memory."""
    t = tier(hw)
    plan = {
        "strong": {"num_ctx": 16384, "keep_alive": "30m"},
        "good": {"num_ctx": 8192, "keep_alive": "15m"},
        "light": {"num_ctx": 4096, "keep_alive": "5m"},
    }[t]
    notes = {
        "strong": "Plenty of power: long memory per answer, models kept warm, full three-head checking.",
        "good": "Comfortable for the three small models. Checking works but is slower.",
        "light": "Modest hardware: shorter memory per answer; use Quick thinking for day-to-day, Careful for important work.",
    }[t]
    return {"tier": t, **plan, "note": notes, "upgrades": [{"model": m, "why": w} for m, w in UPGRADES[t]]}


def pressure() -> dict:
    """How stretched the computer is right now. `low` means: do not start more heavy work."""
    total, avail = memory()
    if not total:
        return {"free_gb": 0.0, "low": False}
    free_gb = avail / GIB
    return {"free_gb": round(free_gb, 1), "low": free_gb < 1.5 or avail / total < 0.06}


def describe(hw: dict) -> str:
    gpu = f"{hw['gpu']} ({hw['vram_gb']:g} GB)" if hw.get("gpu") else "no dedicated graphics card found"
    return f"{hw['cpu']} ({hw['cores']} threads), {hw['ram_gb']:g} GB memory, {gpu}"
