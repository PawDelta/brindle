"""brindle's redaction pass: secrets in agent text are masked where it is
stored and uploaded, and ordinary hashes and ids pass through unchanged."""
import gzip
import io
import json
import logging
import sys

import pytest

from brindle import ci_client, codemap, config, history, redact, watch
from brindle.db import Agent, LearnedRule, Workspace
from brindle.native import NativeAgent, Permissions, Toolbox, core_tools
from brindle.native.client import Reply, ToolCall
from brindle.pro import analysis_docs as ad
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_analysis_docs import remote, uploader  # noqa: F401 - fixture
from test_ci_client import RUN_TOKEN, FakeAdapter, Server
from test_pro_remote_learning import login_as

# Built up at run time, so no scanner takes the test file for a leak.
SLACK = "xoxb-" + "1234567890-" + "AbCdEfGhIjKlMnOp"
PAT = "ghp_" + "A1b2C3d4E5" * 4
TEXT = f"use {SLACK} and {PAT} to post"
LABELLED_KEY = "QwErTyUiOpAsDfGhJkLzXcVbNm"
MULTILINE = "line-one-x9\nline-two-y8"
MASKED = "use [redacted] and [redacted] to post"
KEY, KEY_END = "-----BEGIN " + "PRIVATE KEY-----", "-----END " + "PRIVATE KEY-----"
KEY_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"


def _workspace(db, tmp_path):
    ws = Workspace("proj/feat", str(tmp_path), "feat", "main", "feat", "main", str(tmp_path), None, "t", 1.0)
    db.add_workspace(ws)
    db.add_agent(Agent("sup", ws.id, "supervisor", "shell", None, "interactive", "processing", "", None, 1.0))
    return ws


@pytest.mark.variant("security boundary: secrets are masked in stored messages and in analysis doc uploads")
def test_message_and_analysis_doc_upload_are_masked(tmp_path, db, remote, fixed_identity):
    _workspace(db, tmp_path)
    stored = db.get_message("sup", db.enqueue_held("sup", TEXT, None))

    up, store = uploader(tmp_path, remote, [])
    login_as(remote, store, features=("analysis_docs",))
    up.submit([ad.Doc(files=[{"name": "notes.md", "md": TEXT}], source={"kind": "task", "ref": TEXT})])
    assert up.flush()

    assert stored.body == MASKED
    assert remote.docs[0]["files"] == [{"name": "notes.md", "md": MASKED}]
    assert remote.docs[0]["source"]["ref"] == MASKED


def test_check_output_learned_rule_and_graphify_log_are_masked(tmp_path, db, monkeypatch):
    monkeypatch.setenv("MULTILINE_SECRET", MULTILINE)
    ws = _workspace(db, tmp_path)
    db.set_check(ws.id, "a" * 40, "pytest", False, TEXT)
    db.save_learned_rule(LearnedRule(str(tmp_path), "k1", "dev", TEXT, TEXT, None, "[]", "[]", "suggested", 1.0))
    log = tmp_path / "graphify.log"
    codemap._log_masked(io.BytesIO(f"{TEXT}\nkey {MULTILINE} end\n".encode()), log)
    try:
        raise RuntimeError(TEXT)
    except RuntimeError:
        watch._log_error("sidebar crashed")
        record = logging.LogRecord("w", logging.ERROR, "", 0, TEXT, None, sys.exc_info())
    watch_line = redact.MaskingFormatter("%(message)s").format(record)

    rule = db.get_learned_rule(str(tmp_path), "k1")
    assert db.get_check(ws.id, "a" * 40, "pytest").output == MASKED
    assert (rule.title, rule.rule) == (MASKED, MASKED)
    assert log.read_text() == MASKED + "\nkey [redacted] end\n"
    for text in ((config.brindle_home() / "sidebar.log").read_text(), watch_line):
        assert PAT not in text and SLACK not in text and MASKED in text


@pytest.mark.variant("security boundary: a CI reviewer reply is masked before truncation can split a token, and evidence and event uploads are masked")
def test_ci_reply_is_masked_before_it_is_cut_and_uploads_are_masked(tmp_path):
    reply = PAT + " " + "y" * (ci_client.REPLY_MAX - 20)    # the cut falls inside the token
    adapter = FakeAdapter("claude", reply=reply)
    row, _ = ci_client.ask_reviewer({"id": "r1", "provider": "claude", "instructions": "review"},
                                    str(tmp_path), {"FAKE_KEY": "x"}, {"claude": adapter}, None)
    server = Server(None)
    server.client.put_evidence(RUN_TOKEN, "val_1", {"checks": [{"output_excerpt": TEXT}], "reviews": [row]})
    server.client.events(RUN_TOKEN, "run_1", {"state": "needs_user", "question": TEXT})

    sent = server.evidence_calls[0]
    assert PAT[4:24] not in sent["reviews"][0]["reply"] and sent["reviews"][0]["reply"].endswith("y")
    assert sent["checks"][0]["output_excerpt"] == MASKED
    assert server.events[0]["question"] == MASKED


class _Scripted:
    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, system, messages, tools, **kw):
        return self.replies.pop(0)


@pytest.mark.variant("security boundary: the native agent's transcript and saved state on disk are masked, the live conversation is not")
def test_native_transcript_and_saved_state_are_masked_but_the_live_conversation_is_not(tmp_path):
    (tmp_path / "secret.txt").write_text(f"token {PAT}\n")
    client = _Scripted([Reply("reading", [ToolCall("t1", "Read", {"path": "secret.txt", "API_KEY": LABELLED_KEY})], "tool_calls"),
                        Reply(f"done: {SLACK}", [], "stop")])
    a = NativeAgent(client, Toolbox().add(*core_tools(str(tmp_path))),
                    Permissions("acceptEdits", []), "sys", transcript=tmp_path / "t.jsonl")
    assert a.run("go") == f"done: {SLACK}"
    a.save(tmp_path / "saved.json")

    for persisted in ((tmp_path / "t.jsonl").read_text(), (tmp_path / "saved.json").read_text()):
        assert PAT not in persisted and SLACK not in persisted and LABELLED_KEY not in persisted
        assert "[redacted]" in persisted
    assert PAT in json.dumps([m.get("content") for m in a.messages])


@pytest.mark.parametrize("text, expected", [
    (" ".join(["AKIA" + "ABCDEFGHIJKLMNOP", "AIza" + "S" * 35, "eyJhbGciOiJI.eyJzdWIiOiIx.c2lnbmF0dXJl",
               "squ_" + "0a1b2c3d4e" * 4, "ATATT3xFfGF0" + "Zz9" * 8, "sk_live_" + "Ab1Cd2" * 4,
               "rk_live_" + "Ab1Cd2" * 4, "npm_" + "Ab1Cd2" * 6, "hf_" + "Ab1Cd2" * 6,
               "pypi-AgEIcHlwaS5vcmc" + "Ab1Cd2" * 10, "https://hooks.slack.com/services/" + "T0FAKE/B0FAKE/Ab1Cd2" * 2]),
     " ".join(["[redacted]"] * 11)),
    ("Authorization: Bearer AbCdEfGhIjKlMnOp", "Authorization: Bearer [redacted]"),
    ("see Bearer abc123def456", "see Bearer [redacted]"),
    ("API_KEY=QwErTyUiOpAsDfGhJkLzXcVbNm", "API_KEY=[redacted]"),
    ("DB_PASSWORD=Xk29fLq0Zp7 and token: 1200", "DB_PASSWORD=[redacted] and token: 1200"),
    ("run with my-env-secret-42 set", "run with [redacted] set"),
    ("parallelism false, k9x and on in postgresql", "parallelism false, [redacted] and on in postgresql"),
    ("curl -u admin:kQzRvBnTxLpW pin 31415926535897", "curl -u admin:[redacted] pin [redacted]"),
    ("mysql -uroot -pk9x db", "mysql -uroot -p[redacted] db"),
    ("file tokenmy-env-secret-42x", "file token[redacted]x"),
    ("mysql://app:pw-fake@db:3306/app", "mysql://app:[redacted]@db:3306/app"),
    ('{"content": "' + KEY + "\\n" + KEY_BODY + "\\n" + KEY_BODY + '"}', '{"content": "[redacted]"}'),
    ("pw /opt9-secret here", "pw [redacted] here"),
    ("key file /Users/me/key.pem", "key file /Users/me/key.pem"),
])
def test_mask(text, expected, monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "my-env-secret-42")
    monkeypatch.setenv("MY_PIN_TOKEN", "k9x")
    monkeypatch.setenv("SVC_API_PASSWORD", "kQzRvBnTxLpW")
    monkeypatch.setenv("BANK_PASSWORD", "31415926535897")
    monkeypatch.setenv("MY_PASSWORD", "/opt9-secret")
    monkeypatch.setenv("SERVICE_PASSWORD", "on")
    monkeypatch.setenv("TOKENIZERS_PARALLELISM", "false")
    monkeypatch.setenv("POSTGRES_PASSWORD", "postgres")
    monkeypatch.setenv("MY_TOKEN_ENABLED", "1")
    monkeypatch.setenv("SIGNING_KEY_FILE", "/Users/me/key.pem")
    assert redact.mask(text) == expected


@pytest.mark.variant("ordinary hashes, ids, encodings and prose must never be masked")
def test_no_false_positives():
    text = ("commit 18c6a9b9f0e1d2c3b4a5968778695a4b3c2d1e0f id 123e4567-e89b-12d3-a456-426614174000 "
            '"integrity": "sha512-z4PhNX7vuL3xVChQ1m2AB9Yg5AULVxXcg/SpIdNs6c5H0NE8XYXysP+DGNKHfuwvY7kxvUdBeoGlODJ6+SfaPg==" '
            "fixture dGhpcyBpcyBhIHRlc3QgZml4dHVyZSBzdHJpbmc= basic functionality sort key: createdAtDescendingOrder")
    assert redact.mask(text) == text


def test_archived_transcript_masks_values_json_escapes(tmp_path, db, monkeypatch):
    monkeypatch.setenv("QUOTED_SECRET", 'alpha"bravo-42')
    monkeypatch.setenv("MULTILINE_SECRET", MULTILINE)
    src = tmp_path / "s.jsonl"
    record = {"type": "assistant", "message": {"content": f'got alpha"bravo-42 and {MULTILINE}'},
              "input": {"API_KEY": LABELLED_KEY, "headers": {"Authorization": "Bearer AbCdEfGhIjKlMnOp"}}}
    truncated = json.dumps({"message": {"content": 'alpha"bravo-42'}})[:-1]
    src.write_text(json.dumps(record) + "\nnot json: alpha\"bravo-42\n" + truncated + "\n")
    monkeypatch.setattr(history, "agent_transcripts", lambda db, a: [src])
    monkeypatch.setattr(history, "archive_dir", lambda: tmp_path / "archive")

    [rel] = history._copy_transcripts(db, Agent("w1", "ws", "dev", "shell", None, "handoff", "done", "", None, 1.0))
    lines = gzip.decompress((tmp_path / "archive" / rel).read_bytes()).decode().splitlines()
    archived = json.loads(lines[0])
    assert archived["message"]["content"] == "got [redacted] and [redacted]"
    assert archived["input"] == {"API_KEY": "[redacted]", "headers": {"Authorization": "Bearer [redacted]"}}
    assert lines[1:] == ["not json: [redacted]", redact.REDACTED]


def test_masking_stream_holds_a_secret_split_across_writes_lines_and_flushes(monkeypatch):
    monkeypatch.setenv("MULTILINE_SECRET", MULTILINE)
    out = io.StringIO()
    stream = redact.MaskingStream(out)
    stream.write("Traceback: " + PAT[:10])
    stream.flush()
    stream.write(PAT[10:] + "\n")
    stream.write("tail " + SLACK[:8])
    stream.flush()
    assert out.getvalue() == "Traceback: [redacted]\n"
    stream.write(SLACK[8:])
    stream.finish()
    assert out.getvalue() == "Traceback: [redacted]\ntail [redacted]"
    out.truncate(0)
    out.seek(0)
    pad = "a " * redact._HOLD_MAX                  # one line past the hold limit
    for text in (pad + PAT[:10], PAT[10:] + "\n", "env " + MULTILINE[:12], MULTILINE[12:] + " ok\n"):
        stream.write(text)
    assert out.getvalue() == pad + "[redacted]\nenv [redacted] ok\n"
    key, end, body = KEY, KEY_END, KEY_BODY
    big = (body + "\n") * (4 * redact._HOLD_MAX // len(body))      # past where the stream gives up
    for writes in (["Authorization: Bearer\n", "AbCdEfGhIjKlMnOp\n", "ok\n"],
                   [key + "\n", body + "\n", end + "\n", "ok\n"],
                   [key + "\n", big, body + "\n", end + "\n", "ok\n"],
                   [key + "\n", big, body + "\n", "all ok\n"],         # no END: the log goes on
                   [f"     1\t{key}\n", f"     2\t{body}\n", f"     3\t{body}\n", "all ok\n"],   # cat -n
                   ["Authorization: Bearer" + " " * 4 * redact._HOLD_MAX, "\nAbCdEfGhIjKlMnOp\n", "ok\n"]):
        out = io.StringIO()
        stream = redact.MaskingStream(out)
        for text in writes:
            stream.write(text)
        assert "AbCdEfGhIjKlMnOp" not in out.getvalue() and body not in out.getvalue()
        assert out.getvalue().endswith("ok\n")
