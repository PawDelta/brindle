"""The session diff's blame survives a daemon restart and a stopped run."""

from types import SimpleNamespace

import pytest

from brindle.daemon import session_review


class Stopped(Exception):
    """A computation cut off part way (its deadline, a restart)."""


def test_blame_is_kept_on_disk_across_a_restart_and_a_stopped_run(brindle_home, monkeypatch):
    ws = SimpleNamespace(path=str(brindle_home / "sess"), repo_root=str(brindle_home / "repo"))
    blobs = {"a.py": "1" * 40, "b.py": "2" * 40, "c.py": "3" * 40}
    added = {"a.py": (1, 2), "b.py": (3,), "c.py": (4, 5)}
    chain, touches = [("c1", "mb")], {"c1": frozenset(added)}
    blamed, stop_at = [], []

    def blame(ws_, mb, head, path, lines):
        if path in stop_at:
            raise Stopped(path)
        blamed.append(path)
        return {n: head for n in lines}

    monkeypatch.setattr(session_review, "_blame", blame)
    monkeypatch.setattr(session_review, "_blobs", lambda ws_, mb, head: dict(blobs), raising=False)

    def ask(head):      # a fresh process: nothing in memory
        session_review._forget()
        return session_review._blames(ws, "mb", head, chain, touches, added, None)

    # Stopped at the second file: the first one's blame is kept already.
    stop_at.append("b.py")
    with pytest.raises(Stopped):
        ask("c1")
    assert blamed == ["a.py"]
    # The next run resumes: a.py isn't blamed again.
    stop_at.clear()
    first = ask("c1")
    assert blamed == ["a.py", "b.py", "c.py"]
    # After a restart nothing is blamed again.
    blamed.clear()
    assert ask("c1") == first and blamed == []
    # A new commit touches a.py (back to the same blob, a revert): only a.py is blamed again.
    chain.append(("c2", "c1"))
    touches["c2"] = frozenset({"a.py"})
    got = ask("c2")
    assert blamed == ["a.py"] and got["a.py"][1] == {1: "c2", 2: "c2"} and got["b.py"] == first["b.py"]
