"""The desktop app's pages open without slow work on the request path
(scripts/bench_pages.py times each page's routes on a copy of the DB)."""

from brindle.pro import orgkey
from brindle.pro import work_items as wi

SHA = "a" * 40


def test_integrations_page_reads_each_repos_identity_once(tmp_path, monkeypatch):
    """Settings › Integrations maps every repo brindle knows: its root commit
    (a whole-history git walk) is asked once, and again only when the repo at
    that path is a new one (a new .git)."""
    roots = []
    for name in ("alpha", "beta"):
        (tmp_path / name / ".git").mkdir(parents=True)
        roots.append(str(tmp_path / name))
    asked = []

    def git(root, *args):
        asked.append(root)
        return SHA + "\n"

    monkeypatch.setattr(orgkey, "_git", git)
    monkeypatch.setattr(orgkey, "_ROOT_COMMITS", {}, raising=False)
    monkeypatch.setattr(orgkey.OrgKeys, "get", lambda self, org: orgkey.OrgKey(org, "k1", b"\x01" * 32))

    first = wi.local_repos(roots, "org_1")
    assert sorted(asked) == roots
    asked.clear()
    assert wi.local_repos(roots, "org_1") == first
    assert asked == []

    (tmp_path / "beta" / ".git").rename(tmp_path / "beta" / "old-git")
    (tmp_path / "beta" / ".git").mkdir()
    wi.local_repos(roots, "org_1")
    assert asked == [roots[1]]
