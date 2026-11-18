"""A restart resumes the person's sessions side by side, and each one's resume
listed every process of the machine (procs.stop -> procs.table: two ``ps``
listings with every environment) at once: five at a time took 2.8 s each in
the real daemon's log where the lead session's alone took 0.08 s. Callers at
the same moment now share reads, each served by a read that began after it
came (a read under way began before, and a process table older than the call
must never decide whom to stop)."""

import threading

from brindle import procs


def test_concurrent_process_tables_share_reads(monkeypatch):
    monkeypatch.setattr(procs, "PROC", procs.PROC / "absent")     # the `ps` listing, as on macOS
    reads = []              # the n-th read's number, as its listing says
    first_out, release = threading.Event(), threading.Event()

    def ps(flags):
        if flags == "xeww":     # a read's second listing (the first is "xww")
            return {len(reads): (1, "")}
        n = len(reads) + 1
        reads.append(n)
        if n == 1:          # the first read hangs until the others have come
            first_out.set()
            release.wait()
        return {n: (1, "")}

    monkeypatch.setattr(procs, "_ps", ps)
    wakeups = []
    if hasattr(procs, "_cond"):
        notify = procs._cond.notify_all
        monkeypatch.setattr(procs._cond, "notify_all", lambda: (wakeups.append(1), notify())[1], raising=False)
    seen = {}

    def ask(name):
        seen[name] = next(iter(procs.table()))

    first = threading.Thread(target=ask, args=("first",))
    first.start()
    first_out.wait()
    others = [threading.Thread(target=ask, args=(f"other{i}",)) for i in range(4)]
    for t in others:
        t.start()
    if hasattr(procs, "_arrived"):      # each is waiting for the read after the one under way
        with procs._arrived:
            procs._arrived.wait_for(lambda: procs._waiting == 4)
        # Waiters never wake one another: nothing is notified till a read ends.
        assert wakeups == []
    else:                               # every one reads for itself, none waits
        for t in others:
            t.join()
    release.set()
    for t in [first, *others]:
        t.join()

    assert len(reads) == 2                                  # one read for all four who came meanwhile
    assert seen["first"] == 1                               # the first had the read begun for it
    assert {seen[f"other{i}"] for i in range(4)} == {2}     # none was handed a read begun before it came
