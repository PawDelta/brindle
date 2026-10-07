"""The org profile and rule-pack library (Team feature ``org_profiles``):
fetched, verified and cached like the team policy, and looked up between the
user's own files and the built-ins, with pinned items out of a repo's reach."""

import hashlib
import io
import json
import logging
import stat
import time

import pytest

from brindle import airgap, profiles
from brindle.pro import account, auth, credentials, license, org_profiles
from brindle.profiles import load_profile, load_rule_pack, list_rule_packs, profile_names
from pro_fixtures import (  # noqa: F401 - fixtures
    BASE, TEST_KID, backend, claims, pro_env, sign, signing_key,
)

ORG = "org_team1"
REPO = "/work/client-project"

REVIEWER = "---\nname: org-reviewer\ndescription: the org's reviewer\nprovider: claude\n---\nOrg review rules.\n"
PACK = "---\nname: security/org\ndescription: org security\ndeny_patterns:\n  - eval\\(\n---\nNo eval.\n"


def item(kind, name, text, pinned=False):
    return {"kind": kind, "name": name, "text": text, "pinned": pinned}


def signed_library(key, org, version, profiles_, packs, **over):
    """The backend's answer to GET /orgs/{org}/profiles, signed the way orgs.get_library does."""
    digest = hashlib.sha256(org_profiles.canonical(org, version, profiles_, packs)).hexdigest()
    c = claims(org_id=org, sub="org:" + org, version=version, library_sha256=digest,
               token_use="org_library")
    c.update(over)
    return {"org_id": org, "version": version, "profiles": profiles_, "packs": packs,
            "updated_at": 1, "updated_by": "u", "library_sha256": digest,
            "signed": sign(key, c, header={"typ": org_profiles.TYP}), "expires_at": c["exp"]}


class Library:
    """The library the fake backend serves and an admin edits."""

    def __init__(self, backend):
        self.backend, self.version = backend, 3
        self.profiles = [item("profile", "org-reviewer", REVIEWER)]
        self.packs = [item("pack", "security/org", PACK, pinned=True)]
        self.pushes = []
        self.mutate = None      # callable(answer) -> answer, to serve something wrong

    def answer(self):
        out = signed_library(self.backend.key, ORG, self.version, self.profiles, self.packs)
        return self.mutate(out) if self.mutate else out

    def get(self, form, headers):
        if not self.backend._bearer(headers):
            return 401, {"error": "invalid_token"}
        return 200, self.answer()

    def post(self, form, headers):
        if not self.backend._bearer(headers):
            return 401, {"error": "invalid_token"}
        self.pushes.append(form)
        by = {(i["kind"], i["name"]): i for i in self.profiles + self.packs}
        for d in form.get("delete", []):
            by.pop((d["kind"], d["name"]), None)
        for i in form.get("publish", []):
            by[(i["kind"], i["name"])] = i
        self.profiles = sorted((i for i in by.values() if i["kind"] == "profile"), key=lambda i: i["name"])
        self.packs = sorted((i for i in by.values() if i["kind"] == "pack"), key=lambda i: i["name"])
        self.version += 1
        return 200, {"org_id": ORG, "version": self.version, "profiles": self.profiles, "packs": self.packs}


def login(backend, features=("team", "org_profiles"), **extra):
    t = backend.issue()
    backend.store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                        "access_expires_at": time.time() + 900, "base_url": BASE, "org_id": ORG,
                        "entitlement": sign(backend.key, claims(org_id=ORG, plan="team",
                                                                features=list(features), role="admin")),
                        **extra})
    license.clear_cache()


@pytest.fixture
def lib(backend, monkeypatch):
    backend.store = credentials.default_store()
    library = Library(backend)
    backend.routes[f"GET /orgs/{ORG}/profiles"] = library.get
    backend.routes[f"POST /orgs/{ORG}/profiles"] = library.post
    # The loader builds its own client; point it at the fake backend.
    monkeypatch.setattr(org_profiles, "client_for",
                        lambda base=None, transport=None: auth.Client(BASE, transport or backend))
    login(backend)
    return library


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    (r / ".brindle" / "agents").mkdir(parents=True)
    (r / ".brindle" / "rules").mkdir(parents=True)
    return r


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def fetches(backend):
    return [c for c in backend.calls if c[0] == f"GET /orgs/{ORG}/profiles"]


# -- no entitlement: nothing from the org ----------------------------------------------------------


def test_not_logged_in_means_no_org_items(backend, repo):
    assert org_profiles.items("profile") == {}
    with pytest.raises(KeyError):
        load_profile("org-reviewer", str(repo))
    assert load_profile("developer", str(repo)).name == "developer"
    assert backend.calls == []


def test_a_plan_without_the_feature_fetches_nothing(backend, lib, repo):
    login(backend, features=("team",))
    assert org_profiles.items("profile") == {}
    with pytest.raises(KeyError):
        load_profile("org-reviewer", str(repo))
    assert fetches(backend) == []


# -- lookup order -------------------------------------------------------------------------------


def test_an_org_profile_loads_and_says_where_from(lib, repo):
    p = load_profile("org-reviewer", str(repo))
    assert p.prompt == "Org review rules." and p.description == "the org's reviewer"
    assert profiles.profile_source("org-reviewer", str(repo)) == "org profile org-reviewer"
    assert "org-reviewer" in profile_names(str(repo))
    assert "org-reviewer" in [x.name for x in profiles.list_profiles(str(repo))]


def test_lookup_order_is_repo_user_org_builtin(lib, repo, brindle_home):
    lib.profiles = [item("profile", "developer", "---\nname: developer\nprovider: codex\n---\nOrg dev.\n")]
    # The org's unpinned profile beats the built-in...
    assert load_profile("developer", str(repo)).prompt == "Org dev."
    # ...the user's own beats the org's...
    write(brindle_home / "agents" / "developer.md", "---\nname: developer\n---\nUser dev.\n")
    org_profiles.clear_memo()
    assert load_profile("developer", str(repo)).prompt == "User dev."
    # ...and the repo's beats the user's.
    write(repo / ".brindle" / "agents" / "developer.md", "---\nname: developer\n---\nRepo dev.\n")
    assert load_profile("developer", str(repo)).prompt == "Repo dev."
    assert profiles.profile_source("developer", str(repo)).endswith("agents/developer.md")


def test_a_pinned_profile_cannot_be_shadowed(lib, repo, brindle_home, caplog):
    lib.profiles = [item("profile", "developer", "---\nname: developer\n---\nOrg dev.\n", pinned=True)]
    repo_file = write(repo / ".brindle" / "agents" / "developer.md", "---\nname: developer\n---\nRepo dev.\n")
    write(brindle_home / "agents" / "developer.md", "---\nname: developer\n---\nUser dev.\n")
    with caplog.at_level(logging.WARNING, logger="brindle.profiles"):
        assert load_profile("developer", str(repo)).prompt == "Org dev."
        assert load_profile("developer", str(repo)).prompt == "Org dev."
    warned = [r.getMessage() for r in caplog.records if "pinned" in r.getMessage()]
    assert any(str(repo_file) in w for w in warned)
    assert len([w for w in warned if str(repo_file) in w]) == 1       # once, not per lookup
    assert profiles.profile_source("developer", str(repo)) == "org profile developer"


def test_a_pinned_profile_is_the_parent_a_repo_profile_extends(lib, repo):
    lib.profiles = [item("profile", "base", "---\nname: base\nmodel: opus\n---\nOrg base.\n", pinned=True)]
    write(repo / ".brindle" / "agents" / "base.md", "---\nname: base\nmodel: haiku\n---\nLoose base.\n")
    write(repo / ".brindle" / "agents" / "kid.md", "---\nname: kid\nextends: base\n---\nKid.\n")
    kid = load_profile("kid", str(repo))
    assert kid.model == "opus" and kid.prompt == "Org base.\n\nKid."


def test_rule_packs_follow_the_same_order(lib, repo, brindle_home, caplog):
    lib.packs.append(item("pack", "security/backend", "---\nname: security/backend\n---\nOrg backend.\n"))
    assert load_rule_pack("security/org", str(repo)).source == "org rule pack security/org"
    assert load_rule_pack("security/org", str(repo)).deny_patterns == ["eval\\("]
    assert "security/org" in list_rule_packs(str(repo))
    # Pinned: the repo's pack of that name is ignored.
    repo_pack = write(repo / ".brindle" / "rules" / "security" / "org.md",
                      "---\nname: security/org\n---\nLoose.\n")
    with caplog.at_level(logging.WARNING, logger="brindle.profiles"):
        pack = load_rule_pack("security/org", str(repo))
    assert pack.prompt == "No eval." and pack.deny_patterns
    assert any("pinned" in r.getMessage() and str(repo_pack) in r.getMessage() for r in caplog.records)
    # Unpinned: repo, then user, then org, then built-in.
    assert load_rule_pack("security/backend", str(repo)).prompt == "Org backend."
    write(brindle_home / "rules" / "security" / "backend.md", "---\nname: security/backend\n---\nUser.\n")
    org_profiles.clear_memo()
    assert load_rule_pack("security/backend", str(repo)).prompt == "User."


# -- the signed library ----------------------------------------------------------------------------


def test_the_canonical_form_is_what_the_backend_signs():
    assert org_profiles.canonical("o", 2, [item("profile", "a", "é")], []) == (
        b'{"org_id":"o","packs":[],"profiles":[{"kind":"profile","name":"a","pinned":false,'
        b'"text":"\\u00e9"}],"version":2}')


def test_the_library_is_cached_in_a_private_directory(backend, lib, repo):
    load_profile("org-reviewer", str(repo))
    d = org_profiles.cache_dir(ORG)
    assert d.name == f"profiles-{ORG}" and d.parent.name == "pro"
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    f = d / org_profiles.CACHE_FILE
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert len(fetches(backend)) == 1
    # Later lookups, even after the in-process memo is gone, read the cache.
    org_profiles.clear_memo()
    load_profile("org-reviewer", str(repo))
    assert len(fetches(backend)) == 1


def test_a_new_version_is_fetched_once_the_copy_is_old(backend, lib, repo):
    assert org_profiles.library().version == 3
    lib.version = 4
    ent = license.current()
    later = time.time() + org_profiles.REFRESH_EVERY + 10
    got = org_profiles.current_library(ent, now=later)
    assert got.version == 4 and len(fetches(backend)) == 2


def test_the_last_good_copy_is_used_when_the_fetch_fails(backend, lib, repo):
    org_profiles.library()
    backend.routes[f"GET /orgs/{ORG}/profiles"] = [auth.TransportError("offline")]
    org_profiles.clear_memo()
    got = org_profiles.current_library(license.current(), now=time.time() + 2 * org_profiles.REFRESH_EVERY)
    assert got.version == 3
    # ...but not forever: past the signature's grace it no longer verifies.
    with pytest.raises(org_profiles.LibraryUnavailable):
        org_profiles.current_library(license.current(), now=time.time() + license.DEFAULT_GRACE + 7200)


def test_fail_closed_when_no_library_was_ever_fetched(backend, lib, repo, caplog):
    backend.routes[f"GET /orgs/{ORG}/profiles"] = [(503, {"error": "unavailable"})]
    with caplog.at_level(logging.WARNING, logger="brindle.pro.org_profiles"):
        assert org_profiles.items("profile") == {}
        with pytest.raises(KeyError):
            load_profile("org-reviewer", str(repo))
    assert any("unavailable" in r.getMessage() for r in caplog.records)
    # Local and built-in profiles still work.
    assert load_profile("developer", str(repo)).name == "developer"


def test_a_failed_fetch_is_not_retried_on_every_lookup(backend, lib, repo):
    backend.routes[f"GET /orgs/{ORG}/profiles"] = [(503, {"error": "unavailable"})]
    for _ in range(5):
        assert org_profiles.items("profile") == {}
    assert len(fetches(backend)) == 1


@pytest.mark.parametrize("what", ["digest", "version", "org", "typ", "key", "text", "expired",
                                  "kind", "name"])
def test_an_answer_that_does_not_verify_is_refused(backend, lib, repo, what):
    def bad(answer):
        if what == "digest":
            answer["profiles"][0]["text"] += "x"            # content no longer matches the signature
        elif what == "version":
            answer["version"] = 99
        elif what == "org":
            answer["org_id"] = "org_other"
        elif what == "typ":
            answer["signed"] = sign(backend.key, claims(org_id=ORG, sub="org:" + ORG,
                                                        token_use="org_library"))
        elif what == "key":
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            c = claims(org_id=ORG, sub="org:" + ORG, version=3, token_use="org_library",
                       library_sha256=answer["library_sha256"])
            answer["signed"] = sign(Ed25519PrivateKey.generate(), c, header={"typ": org_profiles.TYP})
        elif what == "text":
            answer["profiles"].append(item("profile", "x", ""))
        elif what == "expired":
            answer.update(signed_library(backend.key, ORG, 3, lib.profiles, lib.packs,
                                         iat=int(time.time()) - 40 * 86400,
                                         exp=int(time.time()) - 39 * 86400))
        elif what == "kind":
            answer["profiles"][0]["kind"] = "pack"
        elif what == "name":
            answer["profiles"][0]["name"] = "../../etc/passwd"
        return answer

    lib.mutate = bad
    assert org_profiles.items("profile") == {}
    with pytest.raises(KeyError):
        load_profile("org-reviewer", str(repo))


def test_an_edited_cache_file_is_not_trusted(backend, lib, repo):
    org_profiles.library()
    f = org_profiles.cache_dir(ORG) / org_profiles.CACHE_FILE
    doc = json.loads(f.read_text())
    doc["response"]["profiles"][0]["text"] = "---\nname: org-reviewer\n---\nLoosened.\n"
    f.write_text(json.dumps(doc))
    f.chmod(0o600)
    assert org_profiles.load_cached(ORG) is None
    # With the backend unreachable that leaves nothing.
    backend.routes[f"GET /orgs/{ORG}/profiles"] = [auth.TransportError("offline")]
    org_profiles.clear_memo()
    assert org_profiles.items("profile") == {}


def test_a_loose_cache_file_is_not_trusted(backend, lib, repo):
    org_profiles.library()
    (org_profiles.cache_dir(ORG) / org_profiles.CACHE_FILE).chmod(0o644)
    assert org_profiles.load_cached(ORG) is None


def test_air_gap_mode_never_fetches(backend, lib, repo, monkeypatch):
    org_profiles.library()
    n = len(fetches(backend))
    monkeypatch.setattr(airgap, "enabled", lambda cfg=None: True)
    ent = license.current()
    got = org_profiles.current_library(ent, now=time.time() + 2 * org_profiles.REFRESH_EVERY)
    assert got.version == 3 and len(fetches(backend)) == n
    (org_profiles.cache_dir(ORG) / org_profiles.CACHE_FILE).unlink()
    with pytest.raises(org_profiles.LibraryUnavailable, match="air-gap"):
        org_profiles.current_library(ent)
    assert len(fetches(backend)) == n


# -- brindle account org profiles ----------------------------------------------------------------------


def run(backend, *args):
    out, err = io.StringIO(), io.StringIO()
    code = account.ProAccount(REPO, store=backend.store, transport=backend, out=out, err=err).run(list(args))
    return code, out.getvalue(), err.getvalue()


def test_list_shows_the_library_and_pins(backend, lib):
    code, out, err = run(backend, "org", "profiles")
    assert code == 0, err
    assert "version 3" in out and "org-reviewer" in out and "security/org  (pinned)" in out
    assert run(backend, "org", "profiles", "list") == run(backend, "org", "profiles")


def test_list_falls_back_to_the_cache(backend, lib):
    run(backend, "org", "profiles")
    backend.routes[f"GET /orgs/{ORG}/profiles"] = [auth.TransportError("offline")]
    code, out, _ = run(backend, "org", "profiles")
    assert code == 0 and "cached" in out and "org-reviewer" in out


def test_list_without_the_feature(backend, lib):
    login(backend, features=("team",))
    code, out, _ = run(backend, "org", "profiles")
    assert code == 0 and "no profile library" in out


def test_push_publishes_a_profile_named_by_its_frontmatter(backend, lib, tmp_path):
    f = write(tmp_path / "Whatever.md", "---\nname: qa-lead\ndescription: d\n---\nQA.\n")
    code, out, err = run(backend, "org", "profiles", "push", str(f))
    assert code == 0, err
    assert lib.pushes == [{"publish": [item("profile", "qa-lead", f.read_text())], "delete": []}]
    assert "Published profile qa-lead" in out and "version 4" in out
    assert [i["name"] for i in lib.profiles] == ["org-reviewer", "qa-lead"]
    # This machine's copy was brought up to date.
    assert org_profiles.load_cached(ORG).version == 4
    assert "qa-lead" in org_profiles.items("profile")


def test_push_a_pinned_pack_with_a_name(backend, lib, tmp_path):
    f = write(tmp_path / "pack.md", "---\ndescription: d\ndeny_patterns: [print\\(]\n---\nNo prints.\n")
    code, out, err = run(backend, "org", "profiles", "push", str(f), "--pack", "--pinned",
                         "--name", "style/quiet")
    assert code == 0, err
    assert lib.pushes[0]["publish"] == [item("pack", "style/quiet", f.read_text(), pinned=True)]
    assert "(pinned)" in out
    assert org_profiles.items("pack")["style/quiet"].pinned


def test_push_without_a_name_uses_the_file_name(backend, lib, tmp_path):
    f = write(tmp_path / "triage.md", "---\ndescription: d\n---\nTriage.\n")
    assert run(backend, "org", "profiles", "push", str(f))[0] == 0
    assert lib.pushes[0]["publish"][0]["name"] == "triage"


@pytest.mark.parametrize("name", ["Bad Name", "UPPER", "-x", "a//b"])
def test_push_refuses_a_name_the_backend_would(backend, lib, tmp_path, name):
    f = write(tmp_path / "x.md", "---\ndescription: d\n---\nText.\n")
    code, _, err = run(backend, "org", "profiles", "push", str(f), "--name", name)
    assert code == 1 and "not a valid org profile name" in err
    assert lib.pushes == []


def test_push_refuses_a_missing_or_huge_file(backend, lib, tmp_path):
    code, _, err = run(backend, "org", "profiles", "push", str(tmp_path / "nope.md"))
    assert code == 1 and "cannot read" in err
    big = write(tmp_path / "big.md", "x" * (org_profiles.MAX_TEXT + 1))
    code, _, err = run(backend, "org", "profiles", "push", str(big), "--name", "big")
    assert code == 1 and "characters" in err
    assert lib.pushes == []


def test_a_refused_push_reports_the_backend_error(backend, lib, tmp_path):
    backend.routes[f"POST /orgs/{ORG}/profiles"] = [(403, {"error": "forbidden"})]
    f = write(tmp_path / "x.md", "---\nname: x\n---\nText.\n")
    code, _, err = run(backend, "org", "profiles", "push", str(f))
    assert code == 1 and "forbidden" in err


def test_rm_deletes_a_profile_or_a_pack(backend, lib):
    code, out, err = run(backend, "org", "profiles", "rm", "org-reviewer")
    assert code == 0, err
    assert lib.pushes[-1] == {"publish": [], "delete": [{"kind": "profile", "name": "org-reviewer"}]}
    assert "Removed profile org-reviewer" in out
    code, out, _ = run(backend, "org", "profiles", "rm", "security/org", "--pack")
    assert code == 0 and lib.pushes[-1]["delete"] == [{"kind": "pack", "name": "security/org"}]
    assert lib.profiles == [] and lib.packs == []
    assert org_profiles.items("profile") == {}


def test_usage_errors(backend, lib, tmp_path):
    f = write(tmp_path / "x.md", "---\nname: x\n---\nText.\n")
    for args in (("push",), ("push", str(f), "extra"), ("rm",), ("rm", "a", "b"), ("frob",),
                 ("list", "extra"), ("rm", "a", "--pinned"), ("list", "--pack"),
                 ("push", str(f), "--name")):
        code, _, err = run(backend, "org", "profiles", *args)
        assert code == 2 and "usage:" in err, args
    assert lib.pushes == []


def test_the_features_page_points_at_the_command(backend, lib):
    out = io.StringIO()
    account.ProAccount(REPO, store=backend.store, transport=backend, out=out).run([])
    assert "brindle account org profiles" in out.getvalue()
