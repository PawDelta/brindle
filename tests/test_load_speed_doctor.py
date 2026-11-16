"""Doctor reuses CLI probes without waiting for background refreshes."""

from types import SimpleNamespace

from brindle import company_identity, company_login, doctor, provider_homes, providers

VERSION = doctor._version


def test_doctor_reuses_probes_until_binary_changes(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_text("first install")
    present = [True]
    stamp = [1]
    calls = []
    jobs = []
    broken = [False]
    clock = [0.0]
    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(doctor, "_version", VERSION)

    class Thread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            jobs.append(self.target)

    monkeypatch.setattr(doctor.threading, "Thread", Thread)
    monkeypatch.setattr(providers, "which_cli", lambda n: str(binary) if present[0] else None)
    monkeypatch.setattr(doctor.shutil, "which", lambda n: str(binary) if present[0] else None)
    monkeypatch.setattr(company_login, "which", lambda n: str(binary) if present[0] else None)
    monkeypatch.setattr(providers, "claude_binary", lambda: "claude")
    monkeypatch.setattr(provider_homes, "launch_env", lambda p: {})
    monkeypatch.setattr(company_identity, "_setup", lambda: None)
    monkeypatch.setattr(company_identity, "_local_setup", lambda: None)
    monkeypatch.setattr(doctor, "signin_providers", lambda: ["claude"] if present[0] else [])
    stat = doctor.Path.stat
    monkeypatch.setattr(doctor.Path, "stat", lambda self, **kw:
                        SimpleNamespace(st_mtime_ns=stamp[0]) if self == binary else stat(self, **kw))

    def read(argv, **kwargs):
        calls.append(tuple(argv))
        if broken[0]:
            raise OSError("broken executable")
        return SimpleNamespace(returncode=0, stdout=f"v{stamp[0]}", stderr="")

    def identity(argv, *args, **kwargs):
        calls.append(tuple(argv))
        return (127, "") if broken[0] else (0, '{"loggedIn": true, "orgId": "acme"}')

    monkeypatch.setattr(doctor.spawn, "read", read)
    monkeypatch.setattr(company_login, "run_cmd", identity)
    steps = doctor._steps
    monkeypatch.setattr(doctor, "_steps", lambda r, db: [
        step for step in steps(r, db) if step[0] in ("claude", "company identity")
    ])
    # Isolate the process cache, while letting this test run on the base too.
    if hasattr(doctor, "_probe_cache"):
        monkeypatch.setattr(doctor, "_probe_cache", {})
        monkeypatch.setattr(doctor, "_probe_pending", set())

    def request():
        return {c.name: c for c in doctor.checks(None, db=object())}

    def finish():
        while jobs:
            jobs.pop(0)()

    request()
    assert calls == []  # Even a cold request leaves CLI work off the reply path.
    finish()
    first = request()
    assert first["claude identity"].level == doctor.OK
    assert "v1" in first["claude"].detail
    assert len(calls) == 2
    finish()
    assert len(calls) == 2

    stamp[0] = 2
    pending = request()
    assert len(calls) == 2
    assert pending["claude identity"].level == doctor.WARN
    request()  # A second open must share the already queued refresh.
    finish()
    assert len(calls) == 4
    assert "v2" in request()["claude"].detail

    present[0] = False
    assert request()["claude"].level == doctor.FAIL
    assert len(calls) == 4
    present[0] = True
    stamp[0] = 3
    broken[0] = True
    request()
    finish()
    failed = request()
    assert failed["claude"].level != doctor.OK
    assert failed["claude identity"].level == doctor.WARN
    assert len(calls) == 6

    broken[0] = False
    # A transient failure must expire even if the executable is unchanged.
    clock[0] += 60
    assert request()["claude"].level == doctor.WARN
    request()
    assert len(calls) == 6
    finish()
    assert len(calls) == 8
    assert request()["claude"].level == doctor.OK
    assert "v3" in request()["claude"].detail
    assert request()["claude identity"].level == doctor.OK

    # A login can change without reinstalling the CLI: refresh its status,
    # returning the previous result immediately while that refresh is queued.
    clock[0] += company_identity.CACHE_SECONDS
    broken[0] = True
    assert request()["claude identity"].level == doctor.OK
    assert len(calls) == 8
    finish()
    assert len(calls) == 9
    assert request()["claude identity"].level == doctor.WARN
