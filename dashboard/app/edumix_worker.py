"""EduMix source worker: runs the supervision loop as its OWN process, independent of any dashboard tab or of the
dashboard process itself (systemd/edumix-worker.service).

Responsibilities: keep the managed browser reachable, navigate to the authorized class, classify page state,
recover with bounded backoff, and publish health to the durable job row. It never starts an encoder, never types
credentials, and honours the operator's explicit Stop (job.desired == 'stopped').

    python -m app.edumix_worker            # run until SIGTERM/SIGINT
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from . import db, edumix, edumix_api, jobs

log = logging.getLogger("zoomdash.edumix.worker")
TICK_SECONDS = 5.0


class Worker:
    def __init__(self, supervisor: "edumix_api.Supervisor | None" = None):
        self.sup = supervisor or edumix_api.Supervisor()
        self.stop_event = asyncio.Event()
        self.job_id: str | None = None

    async def tick(self, now: float | None = None) -> dict | None:
        job = jobs.current()
        if job is None or job["completed_at"] is not None or job["desired"] != "running":
            self.sup.paused = True            # operator stop: do not resurrect the browser
            if job and job["state"] != "stopped":
                jobs.heartbeat(job["id"], os.getpid(), "stopped", edumix.describe("stopped"))
            self.job_id = None
            return None
        if job["id"] != self.job_id:           # new (or newly adopted) job: start its episode cleanly
            self.job_id = job["id"]
            self.sup.mark_started()
        self.sup.paused = False
        err = None
        st = None
        try:
            st = await self.sup.tick(now)
        except Exception as exc:               # the loop must survive anything; record it, redacted
            err = edumix.redact(f"{type(exc).__name__}: {exc}")
            log.warning("worker tick failed: %s", err)
        jobs.heartbeat(job["id"], os.getpid(), (st or {}).get("state", self.sup.state), st, err)
        return st

    def start_up(self) -> str:
        """Reboot-vs-crash decision (see jobs.apply_boot_policy); never starts a broadcast or an encoder."""
        import psutil
        outcome = jobs.apply_boot_policy(str(int(psutil.boot_time())))
        log.info("edumix worker start-up: %s", outcome)
        return outcome

    async def run(self) -> None:
        log.info("edumix worker started (pid %s)", os.getpid())
        self.start_up()
        while not self.stop_event.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self.stop_event.wait(), TICK_SECONDS)
            except asyncio.TimeoutError:
                pass
        log.info("edumix worker stopped cleanly")     # the browser stays with its own unit; only this loop ends


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init_db()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    w = Worker()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, w.stop_event.set)
        except NotImplementedError:            # Windows event loops: Ctrl+C still raises KeyboardInterrupt
            pass
    try:
        loop.run_until_complete(w.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
