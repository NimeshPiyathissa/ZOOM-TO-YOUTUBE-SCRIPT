"""Resource sampling for the EduMix Live Studio health panel and for benchmarks.

Read-only (psutil). CPU is per-process % of ONE core between two samples (100 = one full core), so numbers are
comparable with the encoder measurements already recorded for this project. Process ownership is matched by the
managed browser's DevTools port, never by image name, so an unrelated Chrome is never counted or touched.
"""
from __future__ import annotations

import os
import time

import psutil

from . import config

_cache: dict[int, psutil.Process] = {}     # psutil computes CPU% between calls on the SAME Process object


def _cached(p: psutil.Process) -> psutil.Process:
    old = _cache.get(p.pid)
    try:
        if old is not None and old.create_time() == p.create_time():
            return old
    except psutil.Error:
        pass
    _cache[p.pid] = p
    return p


def _prune(live: set[int]) -> None:
    for pid in [k for k in _cache if k not in live]:
        _cache.pop(pid, None)


def browser_processes() -> list[psutil.Process]:
    """The managed browser's process tree (main process + renderers/GPU/utility), found via its DevTools port flag."""
    flag = f"--remote-debugging-port={int(config.CHROME_DEBUG_PORT)}"
    roots = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = p.info["cmdline"] or []
            if flag in cmd and not any(a.startswith("--type=") for a in cmd):
                roots.append(p)
        except (psutil.Error, OSError):
            continue
    out: dict[int, psutil.Process] = {}
    for r in roots:
        out[r.pid] = r
        try:
            for c in r.children(recursive=True):
                out[c.pid] = c
        except psutil.Error:
            pass
    return list(out.values())


def _group(procs: list[psutil.Process]) -> dict:
    cpu, rss, n = 0.0, 0, 0
    for p in procs:
        p = _cached(p)
        try:
            cpu += p.cpu_percent(None)
            rss += p.memory_info().rss
            n += 1
        except (psutil.Error, OSError):
            continue
    return {"processes": n, "cpu_percent": round(cpu, 1), "rss_mb": round(rss / 2 ** 20, 1)}


def sample() -> dict:
    """One sample. The first call after start-up reports 0% CPU (psutil needs a previous reading)."""
    me = psutil.Process(os.getpid())
    vm = psutil.virtual_memory()
    bprocs = browser_processes()
    fprocs = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower().startswith("ffmpeg")]
    browser, dash, ffmpeg = _group(bprocs), _group([me]), _group(fprocs)
    _prune({p.pid for p in bprocs} | {p.pid for p in fprocs} | {me.pid})
    return {
        "at": time.time(),
        "host": {"cpu_percent": psutil.cpu_percent(None), "cores": psutil.cpu_count(logical=True),
                 "mem_percent": vm.percent, "mem_available_mb": round(vm.available / 2 ** 20),
                 "mem_total_mb": round(vm.total / 2 ** 20),
                 "load1": round(os.getloadavg()[0], 2) if hasattr(os, "getloadavg") else None},
        "browser": browser, "dashboard": dash, "encoder": ffmpeg,
        "health": _health(vm, browser),
    }


def _health(vm, browser: dict) -> dict:
    """A plain verdict for the panel; thresholds are deliberately conservative for a small box."""
    if vm.available < 400 * 2 ** 20:
        return {"level": "bad", "text": "Very little free memory - the browser may be killed by the OS."}
    if vm.percent > 90:
        return {"level": "warn", "text": "Memory is above 90%."}
    if browser["processes"] == 0:
        return {"level": "idle", "text": "The managed browser is not running."}
    if browser["processes"] > 25:
        return {"level": "warn", "text": f"Unusually many browser processes ({browser['processes']}) - possible leak."}
    return {"level": "ok", "text": "Resources look normal."}
