"""The analysis-doc library client: what is uploaded after a merge, and the
cases where nothing is sent."""
import pytest

from brindle import airgap
from brindle.pro import analysis_docs as ad
from brindle.pro import auth, credentials
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_pro_remote_learning import login_as

MAIN = """---
title: Cache hit rate
question: Why is the cache slow?
---
## Results
- hit rate fell to 41% [verified]
- no change in eviction [not checked]

## Conclusion
- the TTL is too short [inference]
"""


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / ".brindle" / "deliverables" / "cache"
    (d / "appendices").mkdir(parents=True)
    (d / "main.md").write_text(MAIN)
    (d / "appendices" / "a.md").write_text("raw numbers")
    return tmp_path


@pytest.fixture
def remote(backend):
    backend.docs, backend.switches = [], {}

    def store(form, headers):
        backend.docs.append(dict(form))
        return 200, {"id": f"d{len(backend.docs)}", "version": 1}

    backend.routes["POST /analysis-docs"] = store
    backend.routes["GET /me/analysis-docs/settings"] = lambda f, h: (200, {"switches": backend.switches})
    return backend


def uploader(tmp_path, remote, notices, **kw):
    store = credentials.FileStore(tmp_path / "pro")
    keys = credentials.FileStore(tmp_path / "pro", account="learning-keys")
    return ad.AnalysisDocs(str(tmp_path), client=auth.Client(None, remote), store=store, key_store=keys,
                           identity=lambda: "a" * 40, protected=kw.pop("protected", lambda files: []),
                           notice=notices.append, start_thread=False), store


def test_merged_deliverable_uploads_once_with_notice(repo, remote, fixed_identity):
    notices = []
    up, store = uploader(repo, remote, notices)
    login_as(remote, store, features=("analysis_docs",))
    docs = ad.collect(str(repo), [".brindle/deliverables/cache/main.md", ".brindle/deliverables/cache/appendices/a.md"])
    assert [d.slug for d in docs] == ["cache"]          # one doc per slug, however many files changed
    up.submit(docs + docs)
    assert up.flush()
    assert len(remote.docs) == 2 and len(notices) == 1
    sent = remote.docs[0]
    assert sent["scope"] == {"kind": "user", "id": "user_1"} and sent["title"] == "Cache hit rate"
    assert sent["appendices"] == [{"name": "a.md", "md": "raw numbers"}]
    assert sent["meta"]["metrics"] == ["hit rate fell to 41% [verified]"]
    assert sent["meta"]["labels"] == {"verified": 1, "inference": 1, "not_checked": 1}
    assert len(sent["repo_key"]) == 64


@pytest.mark.parametrize("case", ["airgap", "protected", "unentitled", "off"])
def test_nothing_sent(case, repo, remote, fixed_identity, monkeypatch):
    protected = (lambda files: files) if case == "protected" else (lambda files: [])
    up, store = uploader(repo, remote, [], protected=protected)
    login_as(remote, store, features=("learning",) if case == "unentitled" else ("analysis_docs",))
    if case == "airgap":
        monkeypatch.setattr(airgap, "enabled", lambda cfg=None: True)
    if case == "off":
        remote.switches = {"store": "off"}
    up.submit(ad.collect(str(repo), [".brindle/deliverables/cache/main.md"]))
    up.flush()
    assert remote.docs == []
