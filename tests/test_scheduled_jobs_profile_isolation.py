"""Regression test: /api/crons must read jobs.json from the *active profile*.

Before the fix, `cron.jobs.list_jobs()` resolved HERMES_HOME from os.environ
at call time, ignoring the WebUI's per-request thread-local profile. So the
Scheduled Jobs panel showed the process-default profile's jobs regardless of
which profile the user had selected in the cookie.

This test writes two distinct jobs.json files (default + a named profile),
then verifies `cron_profile_context` pins the cron.jobs call to the named
profile's file.
"""
import json
import os
import pathlib
import sys
import threading
from unittest import mock

import pytest

# Ensure both repos are importable.
WEBUI_ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENT_ROOT = pathlib.Path(os.environ.get("HERMES_AGENT_ROOT", pathlib.Path.home() / "hermes-agent"))
for p in (str(WEBUI_ROOT), str(AGENT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _write_jobs(home: pathlib.Path, jobs: list):
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps({"jobs": jobs}), encoding="utf-8"
    )


def test_cron_profile_context_pins_profile_home(tmp_path, monkeypatch):
    """The context manager should swap cron.jobs to read from the named profile."""
    default_home = tmp_path / "default_home"
    meow_home = tmp_path / "default_home" / "profiles" / "meow"

    _write_jobs(default_home, [{"id": "d1", "name": "default-job"}])
    _write_jobs(meow_home, [{"id": "m1", "name": "meow-job"}])

    # Point base at default_home; HERMES_HOME env starts at default.
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    from api import profiles as p

    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", default_home)

    # Baseline: no context → default profile.
    from cron.jobs import list_jobs
    # Force cron.jobs to re-evaluate its cached constants for this test run.
    import cron.jobs as _cj
    _cj.HERMES_DIR = default_home
    _cj.CRON_DIR = default_home / "cron"
    _cj.JOBS_FILE = _cj.CRON_DIR / "jobs.json"
    _cj.OUTPUT_DIR = _cj.CRON_DIR / "output"

    jobs_before = list_jobs(include_disabled=True)
    assert any(j["id"] == "d1" for j in jobs_before), \
        f"Expected default-profile job before entering context, got {jobs_before}"

    # Simulate a request with TLS profile = 'meow'.
    p.set_request_profile("meow")
    try:
        with p.cron_profile_context():
            jobs_inside = list_jobs(include_disabled=True)
            assert any(j["id"] == "m1" for j in jobs_inside), \
                f"Expected meow-profile job inside context, got {jobs_inside}"
            assert not any(j["id"] == "d1" for j in jobs_inside), \
                "Default-profile job leaked into meow context"
    finally:
        p.clear_request_profile()

    # After the context exits, we should be back to default.
    jobs_after = list_jobs(include_disabled=True)
    assert any(j["id"] == "d1" for j in jobs_after), \
        f"Expected default-profile job after exiting context, got {jobs_after}"


def test_cron_profile_context_for_home_pins_explicit_home(tmp_path):
    """Thread variant: pin by explicit path (no TLS)."""
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    _write_jobs(home_a, [{"id": "a1", "name": "A"}])
    _write_jobs(home_b, [{"id": "b1", "name": "B"}])

    # Start with env pointing at A.
    prev = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home_a)
    try:
        import cron.jobs as _cj
        _cj.HERMES_DIR = home_a
        _cj.CRON_DIR = home_a / "cron"
        _cj.JOBS_FILE = _cj.CRON_DIR / "jobs.json"
        _cj.OUTPUT_DIR = _cj.CRON_DIR / "output"

        from cron.jobs import list_jobs
        from api.profiles import cron_profile_context_for_home

        assert any(j["id"] == "a1" for j in list_jobs(include_disabled=True))

        with cron_profile_context_for_home(home_b):
            jobs_inside = list_jobs(include_disabled=True)
            assert any(j["id"] == "b1" for j in jobs_inside), jobs_inside
            assert not any(j["id"] == "a1" for j in jobs_inside), jobs_inside

        # Restored to A.
        assert any(j["id"] == "a1" for j in list_jobs(include_disabled=True))
    finally:
        if prev is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev


def test_cron_profile_context_serializes_concurrent_access(tmp_path):
    """The lock must prevent concurrent contexts from interleaving."""
    from api.profiles import cron_profile_context_for_home

    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    home_a.mkdir()
    home_b.mkdir()

    # Ensure the context lock is released between tests.
    from api import profiles as p
    assert not p._cron_env_lock.locked(), \
        "Lock leaked from a previous test"

    observed = []
    barrier = threading.Barrier(2)

    def worker(home, tag):
        barrier.wait()
        with cron_profile_context_for_home(home):
            observed.append(("enter", tag, os.environ["HERMES_HOME"]))
            # If serialization works, the partner thread cannot be inside
            # its own context at this moment.
            observed.append(("exit", tag))

    t1 = threading.Thread(target=worker, args=(home_a, "A"))
    t2 = threading.Thread(target=worker, args=(home_b, "B"))
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Every enter must be immediately followed by its matching exit (no
    # interleaving), because the lock serializes the two contexts.
    assert len(observed) == 4
    first, second, third, fourth = observed
    assert first[0] == "enter" and second[0] == "exit" and first[1] == second[1]
    assert third[0] == "enter" and fourth[0] == "exit" and third[1] == fourth[1]
