import base64
import os
import unicodedata
from pathlib import Path

import pytest

from brindle import attachments, git, identity_lock, providers, view
from brindle.service.dto import AgentDTO
from test_daemon_actions import _send, world  # noqa: F401


def test_attachment_route(db, repo, world, tmp_path, monkeypatch):
    source = tmp_path / "a picture.png"
    source.write_bytes(b"image bytes")
    code, first = _send("POST", "/v1/agents/wk/attachments", {"path": str(source)})
    assert code == 200, first
    saved = Path(first["paths"][0])
    assert saved.parent == repo / ".brindle" / "attachments"
    assert saved.read_bytes() == source.read_bytes()
    payload = b"x" * attachments.MAX_BYTES
    code, second = _send("POST", "/v1/agents/wk/attachments", {
        "name": "../../a picture.png", "data_base64": base64.b64encode(payload).decode()})
    other = Path(second["paths"][0])
    assert code == 200 and other != saved and other.parent == saved.parent
    assert other.name.endswith("-a_picture.png")
    assert other.read_bytes() == payload and saved.read_bytes() == b"image bytes"
    assert git.out(["check-ignore", str(saved), str(other)], repo).splitlines() == [str(saved), str(other)]
    assert first["text"] == providers.PROVIDERS["claude"].format_attachment(str(saved))
    a = db.get_agent("wk")
    assert AgentDTO.model_validate(view.agent_entry(db, a, alive=False)).accepts_files
    db.conn.execute("UPDATE agents SET provider = 'shell' WHERE id = 'wk'")
    db.conn.commit()
    assert _send("POST", "/v1/agents/wk/attachments", {"path": str(source)})[0] == 400
    assert not AgentDTO.model_validate(view.agent_entry(db, db.get_agent("wk"), alive=False)).accepts_files
    db.set_status("wk", "done")
    assert _send("POST", "/v1/agents/wk/attachments", {"path": str(source)})[0] == 409
    db.set_status("wk", "idle")
    monkeypatch.setattr(identity_lock, "locked_by", lambda _: "another identity")
    assert _send("POST", "/v1/agents/wk/attachments", {"path": str(source)})[0] == 409


def test_attachment_refusals(db, repo, world, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"private contents")
    link = tmp_path / "link"
    link.symlink_to(source)
    parent_link = tmp_path / "linked-dir"
    parent_link.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setattr(providers, "_codex_denied_reads", lambda: [str(source)])
    for path in (source, link, parent_link / "source", tmp_path, tmp_path / "missing"):
        code, result = _send("POST", "/v1/agents/wk/attachments", {"path": str(path)})
        assert code in (400, 403) and "private contents" not in str(result)
    hardlink = tmp_path / "hardlink"
    hardlink.hardlink_to(source)
    assert _send("POST", "/v1/agents/wk/attachments", {"path": str(hardlink)})[0] == 400
    hardlink.unlink()
    with monkeypatch.context() as patch:
        patch.setenv("HOME", str(tmp_path))
        credential = tmp_path / ".aws" / "credentials"
        credential.parent.mkdir()
        credential.write_bytes(b"key")
        custom = tmp_path / "Sécret"
        custom.write_bytes(b"key")
        patch.setattr(providers, "_codex_denied_reads", lambda: [str(custom)])
        for spelling in (".aws/credentials", ".AWS/credentials", "SÉCRET",
                         unicodedata.normalize("NFD", custom.name)):
            assert _send("POST", "/v1/agents/wk/attachments", {
                "path": str(tmp_path / spelling)})[0] == 403
            assert not (repo / ".brindle" / "attachments").exists()
    for body in ({}, {"name": "x", "data_base64": "!"}, {"path": str(link), "name": "x"},
                 {"name": [], "data_base64": "YQ=="}, {"path": 1}):
        assert _send("POST", "/v1/agents/wk/attachments", body)[0] == 400
    # Synthetic Windows paths only: an attempted open can read just our fixture.
    with monkeypatch.context() as patch:
        patch.setattr(attachments, "_open_file", lambda _: os.open(source, os.O_RDONLY))
        for path, expected in (
            ("/mnt/c/Users/name/.ssh/id_rsa", 403),
            ("/MNT/C/users/NAME/.AWS/credentials", 403),  # Case-alias bypass.
            ("/windows/d/Users/name/.ssh/id_rsa", 403),  # Custom automount.
            (r"\\?\C:\Users\name\.ssh\id_rsa", 403),  # Extended native alias.
            ("/windows/c/Users/name/.ssh. /id_rsa", 403),  # Win32 trailing-dot/space alias.
            ("/home/name/.ssh/id_rsa", 403),  # A converted WSL UNC share.
            ("/mnt/c/Users/name/AppData/Roaming/gcloud/credentials.db", 403),
            ("/windows/c/Users/name/AppData/Roaming/GitHub CLI/hosts.yml", 403),
            ("/mnt/c/Users/name/AppData/Local/Microsoft/Credentials/token", 403),
            ("/mnt/c/Projects/notes.txt", 200),
            ("/windows/c/Users/name/Documents/notes.txt", 200),
        ):
            code, result = _send("POST", "/v1/agents/wk/attachments", {"path": path})
            assert code == expected, path
            if code == 200:
                saved = Path(result["paths"][0])
                assert saved.read_bytes() == source.read_bytes()
                saved.unlink()
                saved.parent.rmdir()
    monkeypatch.setattr(attachments, "MAX_BYTES", 4)
    monkeypatch.setattr(providers, "_codex_denied_reads", lambda: [])
    secret = tmp_path / "credential"
    secret.write_bytes(b"secret")
    monkeypatch.setattr(attachments.permissions, "credential_paths", lambda: [str(secret)])
    assert _send("POST", "/v1/agents/wk/attachments", {"path": str(secret)})[0] == 403
    monkeypatch.setenv("HOME", str(tmp_path))
    config_secret = tmp_path / ".config" / "other-cli" / "token"
    config_secret.parent.mkdir(parents=True)
    config_secret.write_bytes(b"secret")
    assert _send("POST", "/v1/agents/wk/attachments", {"path": str(config_secret)})[0] == 403
    assert _send("POST", "/v1/agents/wk/attachments", {
        "path": str(tmp_path / ".CONFIG" / "other-cli" / "token")})[0] == 403
    env = repo / ".env.local"
    env.write_bytes(b"secret")
    assert _send("POST", "/v1/agents/wk/attachments", {"path": str(env)})[0] == 403
    assert _send("POST", "/v1/agents/wk/attachments", {"path": str(repo / ".ENV.LOCAL")})[0] == 403
    assert _send("POST", "/v1/agents/wk/attachments", {"path": str(source)})[0] == 413
    assert _send("POST", "/v1/agents/wk/attachments", {
        "name": "x", "data_base64": base64.b64encode(b"large").decode()})[0] == 413
    assert _send("POST", "/v1/agents/wk/attachments", {"name": "x", "data_base64": "A" * 5000})[0] == 413
    assert not (repo / ".brindle" / "attachments").exists()


def test_attachment_atomic_failure(db, repo, world, monkeypatch):
    target = repo / ".brindle" / "attachments"
    target.mkdir(parents=True)
    previous = target / "existing-x.png"
    previous.write_bytes(b"original")

    def refuse(*args, **kwargs):
        raise OSError("write failed")

    for operation in ("fsync", "link"):
        with monkeypatch.context() as patch:
            patch.setattr(attachments.os, operation, refuse)
            code, _ = _send("POST", "/v1/agents/wk/attachments", {"name": "x.png", "data_base64": "YQ=="})
        assert code == 400 and list(target.iterdir()) == [previous]
        assert previous.read_bytes() == b"original"
    monkeypatch.setattr(attachments.secrets, "token_hex", lambda _: "existing")
    assert _send("POST", "/v1/agents/wk/attachments", {"name": "x.png", "data_base64": "YQ=="})[0] == 400
    assert list(target.iterdir()) == [previous] and previous.read_bytes() == b"original"
    target.rename(target.with_name("original"))
    target.symlink_to(target.with_name("original"), target_is_directory=True)
    assert _send("POST", "/v1/agents/wk/attachments", {"name": "x", "data_base64": "YQ=="})[0] == 400


@pytest.mark.parametrize("provider,expected", [
    ("claude", '@"/work tree/a.png"'),
    ("codex", '"/work tree/a.png"'),
    ("antigravity", '"/work tree/a.png"'),
    ("opencode", '"/work tree/a.png"'),
    ("native", '"/work tree/a.png"'),
    ("shell", None), ("subagent", None), ("base", None),
])
def test_provider_attachment(provider, expected):
    p = providers.PROVIDERS.get(provider, providers.Provider())
    if expected is None:
        with pytest.raises(ValueError):
            p.format_attachment("/work tree/a.png")
    else:
        assert p.format_attachment("/work tree/a.png") == expected
