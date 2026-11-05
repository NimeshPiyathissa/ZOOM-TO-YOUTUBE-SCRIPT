"""Durable job state, reboot-recovery policy and the source worker loop. MOCKED: no browser, no network, no encoder -
these prove the logic, not real-world behaviour on a VPS (see docs/edumix-vps.md for what staging must verify)."""
import asyncio

import pytest

from app import config, db, edumix, edumix_api, edumix_worker, jobs

CLASS = "https://mjphysics.lk/class/abc?token=SECRET123"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.init_db()
    yield


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_open_job_stores_path_only_and_never_a_broadcast():
    j = jobs.open_job(CLASS)
    assert j["class_path"] == "/class/abc"
    assert "SECRET123" not in repr(j)
    assert j["desired"] == "running" and j["broadcast"] is False


def test_new_job_closes_previous_and_survives_reload():
    a = jobs.open_job(CLASS)
    b = jobs.open_job("https://mjphysics.lk/class/two")
    assert jobs.get(a["id"])["completed_at"] is not None
    assert jobs.current()["id"] == b["id"]            # "reopening the dashboard" re-attaches to the newest job


def test_stop_is_durable_and_blocks_recovery():
    jobs.open_job(CLASS, resume_on_boot=True)
    assert jobs.recovery_decision(jobs.current())[0]
    jobs.request_stop("admin")
    ok, why = jobs.recovery_decision(jobs.current())
    assert not ok and jobs.current()["stopped_by"] == "admin"


@pytest.mark.parametrize("edit,reason", [
    ({"desired": "stopped"}, "stopped"),
    ({"resume_on_boot": False}, "resume-on-boot"),
    ({"completed_at": 1.0}, "completed"),
    ({"stopped_by": "admin"}, "completed"),
    ({"broadcast": True}, "broadcast"),
])
def test_recovery_policy_rejects_each_ineligible_case(edit, reason):
    job = {"desired": "running", "completed_at": None, "stopped_by": None, "resume_on_boot": True, "broadcast": False}
    assert jobs.recovery_decision(job) == (True, "eligible")
    job.update(edit)
    ok, why = jobs.recovery_decision(job)
    assert not ok and reason in why
    assert jobs.recovery_decision(None)[0] is False


def test_default_job_is_not_resumed_after_reboot():
    jobs.open_job(CLASS)                               # resume_on_boot defaults to off
    assert not jobs.recovery_decision(jobs.current())[0]


def test_heartbeat_liveness_and_no_secrets_in_status():
    j = jobs.open_job(CLASS)
    st = {**edumix.describe("playing"), "page": {"path": "/class/abc", "title": "x"}, "player": {"present": True},
          "cookie": "SESSIONCOOKIE", "capture": {}}
    jobs.heartbeat(j["id"], 4242, "playing", st, now=1000.0)
    cur = jobs.current()
    assert cur["worker_pid"] == 4242 and cur["state"] == "playing"
    assert "SESSIONCOOKIE" not in str(cur)
    assert jobs.worker_alive(cur, now=1010.0)
    assert not jobs.worker_alive(cur, now=1000.0 + jobs.HEARTBEAT_STALE_SECONDS + 1)


def test_capture_stays_disabled_for_a_running_job():
    jobs.open_job(CLASS)
    assert config.EDUMIX_CAPTURE_ENABLED is False
    assert edumix.capture_status("playing")["enabled"] is False


class FakeSup(edumix_api.Supervisor):
    def __init__(self):
        super().__init__()
        self.ticks = 0
        self.started = 0

    def mark_started(self, now=None):
        self.started += 1
        super().mark_started(now)

    async def tick(self, now=None):
        self.ticks += 1
        self.state = "playing"
        return {**edumix.describe("playing"), "page": {"path": "/class/abc"}, "player": {"present": True}}


def test_worker_runs_without_any_dashboard_and_publishes_health():
    j = jobs.open_job(CLASS)
    sup = FakeSup()
    w = edumix_worker.Worker(sup)
    run(w.tick()); run(w.tick())
    assert sup.ticks == 2 and sup.started == 1        # one episode per job, not per tick
    cur = jobs.get(j["id"])
    assert cur["state"] == "playing" and jobs.worker_alive(cur)


def test_worker_honours_explicit_stop_and_does_not_resurrect():
    jobs.open_job(CLASS)
    sup = FakeSup(); w = edumix_worker.Worker(sup)
    run(w.tick())
    jobs.request_stop("admin")
    assert run(w.tick()) is None
    assert sup.paused and sup.ticks == 1
    run(w.tick())
    assert sup.ticks == 1


def test_worker_survives_a_failing_tick_and_redacts_the_error():
    j = jobs.open_job(CLASS)

    class Boom(FakeSup):
        async def tick(self, now=None):
            raise RuntimeError("probe failed otp=123456")

    w = edumix_worker.Worker(Boom())
    run(w.tick())
    err = jobs.get(j["id"])["last_error"]
    assert err and "123456" not in err


def test_worker_idles_when_there_is_no_job():
    sup = FakeSup(); w = edumix_worker.Worker(sup)
    assert run(w.tick()) is None and sup.ticks == 0


def test_dashboard_does_not_run_its_own_supervisor_when_worker_is_external():
    src = open(edumix_api.__file__.replace("edumix_api.py", "main.py"), encoding="utf-8").read()
    assert "if not config.EDUMIX_WORKER_EXTERNAL:" in src and "supervisor.run()" in src


# ------------------------------------------------------------------ reboot vs crash

def test_crash_restart_adopts_the_job_unchanged():
    jobs.open_job(CLASS)
    jobs.apply_boot_policy("boot-1")                               # first ever start just records the marker
    assert jobs.apply_boot_policy("boot-1") == "adopted"           # same boot: systemd restarted the worker
    assert jobs.current()["desired"] == "running"


def test_reboot_closes_a_default_job_instead_of_resurrecting_it():
    jobs.open_job(CLASS)                                          # resume_on_boot off
    jobs.apply_boot_policy("boot-1")
    out = jobs.apply_boot_policy("boot-2")
    assert out.startswith("closed") and "resume-on-boot" in out
    j = jobs.current()
    assert j["desired"] == "stopped" and j["stopped_by"] == "reboot-policy" and j["completed_at"]


def test_reboot_resumes_only_an_opted_in_source_job():
    jobs.open_job(CLASS, resume_on_boot=True)
    jobs.apply_boot_policy("boot-1")
    assert jobs.apply_boot_policy("boot-2") == "resumed"
    assert jobs.current()["desired"] == "running"


def test_reboot_never_resumes_a_stopped_or_completed_job():
    jobs.open_job(CLASS, resume_on_boot=True)
    jobs.apply_boot_policy("boot-1")
    jobs.request_stop("admin")
    assert jobs.apply_boot_policy("boot-2") == "idle"
    assert jobs.current()["stopped_by"] == "admin"                 # the operator's stop is not overwritten


def test_reboot_never_resumes_a_broadcast_job():
    j = jobs.open_job(CLASS, resume_on_boot=True)
    with db.get_conn() as c:
        c.execute("UPDATE edumix_jobs SET broadcast=1 WHERE id=?", (j["id"],))
    jobs.apply_boot_policy("boot-1")
    assert jobs.apply_boot_policy("boot-2").startswith("closed")
