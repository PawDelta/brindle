"""Settings must resolve profiles once per library per request, not per consumer."""

from collections import Counter
from types import SimpleNamespace

from starlette.requests import Request

from brindle import agents, config, git, profiles, spawn, tmux
from brindle.daemon import routes_read
from brindle.db import Agent, Workspace
from brindle.pro import license


def test_settings_share_profile_resolution_and_see_edits_on_next_read(db, repo, monkeypatch):
    reads, builds = Counter(), Counter()
    original_read, original_build = profiles._builtin_text, profiles._build

    def build(meta, body, name):
        builds[name] += 1
        return original_build(meta, body, name)

    def read(package, name):
        reads[package, name] += 1
        return original_read(package, name)

    monkeypatch.setattr(profiles, "_builtin_text", read)
    monkeypatch.setattr(profiles, "_build", build)
    directories = Counter()
    original_dirs = profiles._with_repo_add_dirs

    def with_dirs(profile, root):
        directories[profile.name, root] += 1
        return original_dirs(profile, root)

    monkeypatch.setattr(profiles, "_with_repo_add_dirs", with_dirs)
    licensing = []
    original_license = license._disk_cache_read

    def entitlement(*args):
        licensing.append(args)
        return original_license(*args)

    monkeypatch.setattr(license, "_disk_cache_read", entitlement)
    # Fake diagnostic and git commands; the bounds measure profile work,
    # independent of the host's tools and speed.
    monkeypatch.setattr(spawn, "read", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout="", stderr=""))
    identities = []
    monkeypatch.setattr(git, "identity_env", lambda *a:
                        identities.append(a) or {"GIT_AUTHOR_NAME": "worker"})
    inherited = Counter()

    def environment(session):
        inherited[session] += 1
        return {}

    monkeypatch.setattr(tmux, "inherited_env", environment)
    library = config.user_profiles_dir()
    library.mkdir(parents=True, exist_ok=True)
    parent = library / "speed-parent.md"
    (library / "speed-child.md").write_text("---\nextends: speed-parent\n---\nChild prompt\n")

    for handler, model, query in (
        (routes_read.get_doctor, "sonnet", "learned=0"),
        (routes_read.get_routing, "sonnet", "learned=0"),
        (routes_read.get_routing, "haiku", "learned=0"),
        (routes_read.get_routing, "haiku", ""),
    ):
        parent.write_text(f"---\nextends: developer\nmodel: {model}\n---\nParent prompt\n")
        reads.clear()
        builds.clear()
        identities.clear()
        inherited.clear()
        directories.clear()
        licensing.clear()
        request = Request({"type": "http", "path": "/v1/settings-probe",
                           "query_string": f"repo={repo}&scope=user&{query}".encode()})
        body = handler.__wrapped__(db, request)
        if handler is routes_read.get_routing:
            row = next(p for p in body["profiles"] if p["name"] == "speed-child")
            assert row["model"] == model
            assert row["role"] == "developer"
        else:
            assert body
        # Lint, cost tier, role inheritance and policy revisit these profiles.
        # The base reread developer 61 times on the fast routing request;
        # Doctor built 274 profiles from a library of 26.
        # The full learner preview checks the repo library too, so at most
        # two scopes resolve the same named profile.
        bound = 1 if query else 2
        assert builds and max(builds.values()) <= bound, builds.most_common(5)
        assert reads and max(reads.values()) <= bound, reads.most_common(5)
        assert identities == []
        assert all(n <= 1 for n in inherited.values()), inherited
        if not query:
            assert sum(inherited.values()) == 1
        assert max(directories.values()) <= 1, directories.most_common(5)
        assert len(licensing) <= 2

    # A preview's cached pane environment must not remove git preparation
    # from a subsequent real launch of the same agent.
    ws = Workspace("preview", str(repo), "preview", "main", "HEAD", None,
                   str(repo), None, "", 0.0)
    agent = Agent("preview", ws.id, "developer", "claude", None, "assign",
                  "starting", "", None, 0.0)
    identities.clear()
    with agents.reading_launch_environment():
        agents._window_environment(agent, ws, None)
    assert identities == []
    launch = agents._window_environment(agent, ws, None)[0]
    assert len(identities) == 1
    assert launch["GIT_AUTHOR_NAME"] == "worker"
