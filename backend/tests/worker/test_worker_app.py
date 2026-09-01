"""Worker app wiring: disabled by default in the app the fixtures build,
lifespan start/stop when enabled, supervisor loop behavior, and the health
endpoint's worker liveness check."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.v1.health as health
from app.config import settings
from app.db import utcnow
from app.main import create_app
from app.services.worker.supervisor import WorkerSupervisor, current, set_current


@pytest.fixture(autouse=True)
def _clean_worker_registry():
    """Never leak a supervisor (real or stand-in) across tests."""
    set_current(None)
    yield
    set_current(None)


class TestDisabledByDefault:
    def test_health_reports_worker_disabled(self, client):
        r = client.get("/api/v1/system/health")
        assert r.status_code == 200
        body = r.json()
        assert body["checks"]["worker"]["status"] == "disabled"
        # A disabled worker is a deliberate configuration: top-level stays ok.
        assert body["status"] == "ok"

    def test_app_lifespan_spawns_no_worker(self, client):
        # The client fixture has already entered/exited the app lifespan.
        assert settings.WORKER_ENABLED is False
        assert current() is None


class TestLifespanWhenEnabled:
    def test_lifespan_starts_and_stops_worker(self, monkeypatch):
        monkeypatch.setattr(settings, "WORKER_ENABLED", True)
        events = []

        class FakeSupervisor:
            async def stop(self):
                events.append("stopped")

        async def fake_start_worker(settings_arg, *, session_factory, gateway):
            events.append(("started", settings_arg is settings))
            return FakeSupervisor()

        monkeypatch.setattr("app.main.start_worker", fake_start_worker)
        app = create_app()
        with TestClient(app):
            assert events == [("started", True)]
        assert events == [("started", True), "stopped"]

    def test_lifespan_skips_worker_when_disabled(self, monkeypatch):
        called = []

        async def fake_start_worker(*args, **kw):
            called.append(1)
            raise AssertionError("must not be called")

        monkeypatch.setattr("app.main.start_worker", fake_start_worker)
        app = create_app()
        with TestClient(app):
            pass
        assert called == []


class TestWorkerHealthCheck:
    def test_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "WORKER_ENABLED", False)
        check = health._worker_check()
        assert check.status == "disabled"

    def test_enabled_without_tick_is_down(self, monkeypatch):
        monkeypatch.setattr(settings, "WORKER_ENABLED", True)
        set_current(None)
        check = health._worker_check()
        assert check.status == "down"

    def test_fresh_tick_is_ok(self, monkeypatch):
        monkeypatch.setattr(settings, "WORKER_ENABLED", True)
        set_current(SimpleNamespace(last_tick_at=utcnow(), last_error=None))
        check = health._worker_check()
        assert check.status == "ok"
        assert "last tick" in (check.detail or "")

    def test_stale_tick_is_degraded(self, monkeypatch):
        monkeypatch.setattr(settings, "WORKER_ENABLED", True)
        monkeypatch.setattr(settings, "WORKER_TICK_SECONDS", 30.0)
        stale = utcnow() - timedelta(seconds=3600)
        set_current(SimpleNamespace(last_tick_at=stale, last_error=None))
        check = health._worker_check()
        assert check.status == "degraded"
        assert "stale" in (check.detail or "")

    def test_last_error_surfaces_in_detail(self, monkeypatch):
        monkeypatch.setattr(settings, "WORKER_ENABLED", True)
        set_current(
            SimpleNamespace(last_tick_at=utcnow(), last_error="RuntimeError: boom")
        )
        check = health._worker_check()
        assert check.status == "ok"  # loop alive; the error is reported, not hidden
        assert "boom" in (check.detail or "")


class TestSupervisorLoop:
    def test_ticks_until_stopped(self):
        class FakeWorker:
            def __init__(self):
                self.ticks = 0

            def tick(self):
                self.ticks += 1

        async def main():
            worker = FakeWorker()
            supervisor = WorkerSupervisor(worker, tick_seconds=0.02)
            await supervisor.start()
            assert current() is supervisor
            await asyncio.sleep(0.11)
            await supervisor.stop()
            assert worker.ticks >= 2
            assert supervisor.last_tick_at is not None
            assert supervisor.last_error is None
            assert current() is None  # stop() deregisters

        asyncio.run(main())

    def test_failing_tick_is_recorded_and_loop_continues(self):
        class FlakyWorker:
            def __init__(self):
                self.ticks = 0

            def tick(self):
                self.ticks += 1
                if self.ticks == 1:
                    raise RuntimeError("transient boom")

        async def main():
            worker = FlakyWorker()
            supervisor = WorkerSupervisor(worker, tick_seconds=0.02)
            await supervisor.start()
            await asyncio.sleep(0.09)
            await supervisor.stop()
            assert worker.ticks >= 2  # the loop survived the failure
            assert supervisor.last_error is None  # cleared by the later good tick

        asyncio.run(main())
