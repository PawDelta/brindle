"""brindle's redaction pass: secrets in agent text are masked where it is
stored and uploaded, and ordinary hashes and ids pass through unchanged."""
import io

import pytest

from brindle import redact
from brindle.db import Agent, Workspace
from brindle.pro import analysis_docs as ad
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_analysis_docs import remote, uploader  # noqa: F401 - fixture
from test_pro_remote_learning import login_as

# Built up at run time, so no scanner takes the test file for a leak.
SLACK = "xoxb-" + "1234567890-" + "AbCdEfGhIjKlMnOp"
PAT = "ghp_" + "A1b2C3d4E5" * 4


def test_message_and_analysis_doc_upload_are_masked(tmp_path, db, remote, fixed_identity):
    text = f"use {SLACK} and {PAT} to post"
    ws = Workspace("proj/feat", str(tmp_path), "feat", "main", "feat", "main", str(tmp_path), None, "t", 1.0)
    db.add_workspace(ws)
    db.add_agent(Agent("sup", ws.id, "supervisor", "shell", None, "interactive", "processing", "", None, 1.0))
    stored = db.get_message("sup", db.enqueue_held("sup", text, None))

    up, store = uploader(tmp_path, remote, [])
    login_as(remote, store, features=("analysis_docs",))
    up.submit([ad.Doc(files=[{"name": "notes.md", "md": text}], source={"kind": "task", "ref": text})])
    assert up.flush()

    masked = "use [redacted] and [redacted] to post"
    assert stored.body == masked
    assert remote.docs[0]["files"] == [{"name": "notes.md", "md": masked}]
    assert remote.docs[0]["source"]["ref"] == masked


@pytest.mark.parametrize("text, expected", [
    (" ".join(["AKIA" + "ABCDEFGHIJKLMNOP", "AIza" + "S" * 35, "eyJhbGciOiJI.eyJzdWIiOiIx.c2lnbmF0dXJl",
               "squ_" + "0a1b2c3d4e" * 4, "ATATT3xFfGF0" + "Zz9" * 8]), " ".join(["[redacted]"] * 5)),
    ("Authorization: Bearer abc123def456", "Authorization: Bearer [redacted]"),
    ("DB_PASSWORD=Xk29fLq0Zp7 and token: 1200", "DB_PASSWORD=[redacted] and token: 1200"),
    ("run with my-env-secret-42 set", "run with [redacted] set"),
])
def test_mask(text, expected, monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "my-env-secret-42")
    assert redact.mask(text) == expected


@pytest.mark.variant("ordinary hashes, ids and encodings must never be masked")
def test_no_false_positives():
    text = ("commit 18c6a9b9f0e1d2c3b4a5968778695a4b3c2d1e0f id 123e4567-e89b-12d3-a456-426614174000 "
            '"integrity": "sha512-z4PhNX7vuL3xVChQ1m2AB9Yg5AULVxXcg/SpIdNs6c5H0NE8XYXysP+DGNKHfuwvY7kxvUdBeoGlODJ6+SfaPg==" '
            "fixture dGhpcyBpcyBhIHRlc3QgZml4dHVyZSBzdHJpbmc= basic functionality")
    assert redact.mask(text) == text


def test_masking_stream_catches_a_token_split_across_writes():
    out = io.StringIO()
    stream = redact.MaskingStream(out)
    stream.write("Traceback: " + PAT[:10])
    stream.write(PAT[10:] + "\n")
    stream.write("tail " + SLACK)
    stream.flush()
    assert out.getvalue() == "Traceback: [redacted]\ntail [redacted]"
