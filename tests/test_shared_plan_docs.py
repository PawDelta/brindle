"""A plan shared through the Library (brindle.pro.shared_docs), against a fake
of the API in pawdelta-web's docs/shared-designs-api.md."""

import asyncio
import base64
import copy
import dataclasses
import functools
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.testclient import TestClient

from brindle import identity_lock, mcp_server, plan_docs, plans, tasks, visual_offers
from brindle.crdt import SharedDoc
from brindle.daemon import holds
from brindle.daemon.app import build_app
from brindle.pro import analysis_docs as ad
from brindle.pro import auth, credentials, license
from brindle.pro import shared_docs as sd
from brindle.service.errors import ServiceError
from pro_fixtures import BASE as BASE_URL, backend, claims, fixed_identity, pro_env, sign, signing_key  # noqa: F401
from test_plans import AUTH, DECISIONS, TOKEN, messages, session  # noqa: F401 - fixture
from test_pro_remote_learning import login_as

DOC = "doc_1"
BASE = f"/analysis-docs/{DOC}"
DESIGN = f"{BASE}/design"
LIST = "/analysis-docs?scope_kind=user&scope_id=user_1&session=boss&design=true"
TEAM ={"kind": "group", "id": "t_1"}
TEAM_LOGIN = ("analysis_docs", "analysis_docs_team")
SUMMARY = "Workers run on a machine the customer controls."


class Library:
    """The Library's one design: the head, its review state, and a compare
    on the revision on screen, as the server does."""

    def __init__(self, remote):
        self.rev, self.content, self.threads, self.votes = 0, None, [], {}
        self.permissions, self.reviewers, self.create_status, self.share_status = {}, [], 200, 200
        self.lose_answer, self.lose_create, self.list_status, self.plan = False, False, 200, None
        self.sign_offs, self.author, self.refuse_accept = [], "u_e", False
        self.answer_status, self.refuse_suggest = 200, False     # once committed, answer this; refuse before committing
        self.refuse_vote, self.draft = False, None
        remote.routes.update({
            f"GET {DESIGN}/draft": lambda f, h: (200, {"rev": self.rev, "frozen": None, "snapshot": base64.b64encode(
                self.draft.export_snapshot()).decode()}),
            **{f"POST {DESIGN}/threads/{n}": functools.partial(self.act, n) for n in range(1, 12)},
            f"POST {DESIGN}/comments": self.comment, f"POST {DESIGN}/suggestions": self.suggest,
            f"POST {DESIGN}/votes": self.vote,
            f"POST {DESIGN}/sign-offs": self.sign_off,
            "POST /analysis-docs": self.create, f"POST {BASE}/share": self.share, f"GET {LIST}": self.list,
            f"GET {DESIGN}": lambda f, h: (200, self.view()), f"POST {DESIGN}/revisions": self.revise,
            f"PUT {DESIGN}/permissions": self.set_permissions, f"PUT {DESIGN}/reviewers": self.set_reviewers,
            f"PUT {DESIGN}/folder": lambda f, h: (200, self.view()),
        })

    def view(self):
        out = {**copy.deepcopy(self.content), "rev": self.rev, "author": self.author,
               "threads": copy.deepcopy(self.threads), "draft": self.draft and {"base_rev": self.rev, "frozen": None},
               "permissions": dict(self.permissions), "reviewers": list(self.reviewers),
               "sign_offs": list(self.sign_offs), "policy_reviewers": [],
               "pending_reviewers": [r["sub"] for r in self.reviewers if r["required"] and not any(
                   s["sub"] == r["sub"] and s["rev"] == self.rev for s in self.sign_offs)]}
        for d in out["decisions"]:
            d["votes"] = self.votes.get(d["id"], {})
            d["confirmed"] = next((o for o, subs in d["votes"].items() if "user_1" in subs), None)
        return {**out, "ready": not out["pending_reviewers"]}

    def answer(self):
        if self.lose_answer:
            raise auth.AuthError("offline", code="network")
        if self.answer_status != 200:
            return self.answer_status, {"error": "server_error"}
        return 200, self.view()

    def next_id(self):
        return max([t["id"] for t in self.threads] + [r["id"] for t in self.threads for r in t["replies"]],
                   default=0) + 1

    def comment(self, form, headers):
        if form["rev"] != self.rev:
            return 409, {"error": "stale", "rev": self.rev}
        item = {"id": self.next_id(), "author": "user_1", "text": form["text"], "at": 9.0,
                "from_supervisor": form.get("from_supervisor", False)}
        if "parent" in form:
            next(t for t in self.threads if t["id"] == form["parent"])["replies"].append(item)
        else:
            self.threads.append({**item, "kind": "comment", "rev": form["rev"], "status": "open", "replies": [],
                                 "anchor": {**form["anchor"], "state": "current"},
                                 "question": form.get("question", False)})
        return self.answer()

    def suggest(self, form, headers):
        if self.refuse_suggest:
            return 503, {"error": "unavailable"}
        self.threads.append({"id": self.next_id(), "author": "user_1", "text": form["text"], "kind": "suggestion",
                             "rev": form["rev"], "status": "open", "replies": [], "after": form["after"], "at": 9.0,
                             "anchor": {"kind": "text", "section": form["section"], "start": form["start"],
                                        "end": form["start"] + len(form["before"]), "quote": form["before"],
                                        "state": "current", **({"cursor": form["cursor"]} if "cursor" in form else {})}})
        return self.answer()

    def vote(self, form, headers):
        if self.refuse_vote:
            return 403, {"error": "policy_denied"}
        options = self.votes.setdefault(form["decision"], {})
        for subs in options.values():
            subs[:] = [s for s in subs if s != "user_1"]
        options.setdefault(form["option"], []).append("user_1")
        return self.answer()

    def act(self, thread_id, form, headers):
        """As the server: only a comment is resolved, and an accept makes the revision itself."""
        if form["rev"] != self.rev:
            return 409, {"error": "stale", "rev": self.rev}
        t, action = next(t for t in self.threads if t["id"] == thread_id), form["action"]
        if action in ("resolve", "reopen"):
            if t["kind"] != "comment":
                return 422, {"error": "invalid_thread"}
            t["status"] = "resolved" if action == "resolve" else "open"
        elif action == "reject":
            t["status"] = "rejected"
        elif self.draft is not None:      # its text is in the draft already, or it is refused
            if not form.get("in_draft"):
                return 409, {"error": "live_draft"}
            t["status"] = "accepted"
        else:
            if self.refuse_accept:
                return 409, {"error": "stale", "rev": self.rev}
            section, a = next(s for s in self.content["sections"] if s["id"] == t["anchor"]["section"]), t["anchor"]
            body = section["markdown"]
            section["markdown"] = body[:a["start"]] + t["after"] + body[a["start"] + len(a["quote"]):]
            t["status"], self.rev, self.votes = "accepted", self.rev + 1, {}
        return self.answer()

    def create(self, form, headers):
        if self.create_status != 200:
            return self.create_status, {"error": "switch_off"}
        self.rev, self.content, self.plan = 1, copy.deepcopy(dict(form["design"])), form["source"]["ref"]
        if self.lose_create:
            raise auth.AuthError("offline", code="network")
        return 200, {"id": DOC, "version": 1}

    def list(self, form, headers):
        if self.list_status != 200:
            return self.list_status, {"error": "unavailable"}
        return 200, {"docs": [{"doc_id": DOC, "created_at": 1, "source": {"kind": "goal", "ref": self.plan}}]
                     if self.plan else []}

    def share(self, form, headers):
        return self.share_status, {"id": DOC} if self.share_status == 200 else {"error": "too_many_scopes"}

    def revise(self, form, headers):
        if self.draft is not None:
            return 409, {"error": "live_draft"}
        if form["rev"] != self.rev:
            return 409, {"error": "stale", "rev": self.rev}
        self.rev, self.content = self.rev + 1, {k: copy.deepcopy(v) for k, v in form.items() if k != "rev"}
        self.votes = {}                                    # votes belong to the revision they were cast on
        if self.lose_answer:
            raise auth.AuthError("offline", code="network")
        return 200, self.view()

    def open_draft(self):
        """As the relay: the head's text, seeded once, becomes the live draft."""
        self.draft = SharedDoc.new({"lede": self.content["lede"],
                                    **{s["id"]: s["markdown"] for s in self.content["sections"]}})

    def freeze(self):
        """The author freezes the draft: its text is the next version."""
        self.content["lede"] = self.draft.text("lede")
        for s in self.content["sections"]:
            s["markdown"] = self.draft.text(s["id"])
        self.rev, self.votes = self.rev + 1, {}

    def set_permissions(self, form, headers):
        self.permissions = dict(form["permissions"])
        return 200, self.view()

    def set_reviewers(self, form, headers):
        self.reviewers = list(form["reviewers"])
        return 200, self.view()

    def sign_off(self, form, headers):
        self.sign_offs.append({"rev": form["rev"], "sub": "user_1", "at": 9.0})
        return 200, self.view()


@pytest.fixture
def parts(db, session, tmp_path, backend):
    """A Team login, the fake Library and an open plan."""
    boss, _ = session
    store = credentials.FileStore(tmp_path / "pro")
    login_as(backend, store, features=TEAM_LOGIN)
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = plans.propose(db, boss, "# Cloud workers", DECISIONS,
                        sections=[{"id": "summary", "title": "Summary", "markdown": SUMMARY}])["id"]
    return pid, Library(backend), {"client": auth.Client(None, backend), "store": store}


def sent(backend, key):
    return [form for k, form, _ in backend.calls if k == key]


def test_ask_the_supervisor_routes_to_the_teammates_session(db, parts, monkeypatch):
    from dataclasses import replace
    from brindle.pro import doc_questions as dq

    pid, lib, net = parts
    sd.share(db, pid, TEAM, **net)
    boss = db.get_agent("boss")
    own = replace(boss, id="my-supervisor", provider="codex")
    db.add_agent(own)
    dq.remember_session(db, own, owner="user_1")
    dq.remember_session(db, boss, owner="author")
    lib.author = "author"
    lib.comment({"rev": 1, "anchor": {"kind": "section", "section": "summary"},
                 "text": "Why these workers?", "question": True}, {})
    repo = db.get_workspace(own.workspace_id).repo_root
    first = dq.dispatch(db, DOC, 1, repo_root=repo, **net)
    assert first["agent_id"] == own.id
    assert dq.dispatch(db, DOC, 1, repo_root=repo, **net) == first
    assert db.pending_count(own.id) == 1 and not messages(db)
    with pytest.raises(ServiceError):
        dq.reply(db, boss, DOC, 1, "Not my question.", **net)
    dq.reply(db, own, DOC, 1, "They keep execution on your machines.", **net)
    assert lib.threads[0]["replies"][0]["from_supervisor"] is True
    assert lib.threads[0]["replies"][0]["author"] == "user_1"


@pytest.mark.parametrize("provider,failed", [
    ("codex", False),
    pytest.param("claude", False, marks=pytest.mark.variant(
        "claude is the default provider: its resume path must be taken, not another provider's")),
    ("antigravity", False), ("opencode", False),
    (None, False), ("claude", True), ("claude", "blocked"),
])
def test_ask_the_supervisor_without_a_session(db, parts, monkeypatch, provider, failed):
    from brindle import agents, antigravity, codex_hook, mcp_server, opencode, preflight, provider_homes, providers
    from brindle.pro import doc_questions as dq
    from brindle.service import sessions

    pid, lib, net = parts
    sd.share(db, pid, TEAM, **net)
    lib.author = "author"
    lib.comment({"rev": 1, "anchor": {"kind": "section", "section": "summary"},
                 "text": "Why these workers?", "question": True}, {})
    monkeypatch.setattr(sessions, "default_provider", lambda repo: provider)
    monkeypatch.setattr(agents, "model_problem", lambda *a, **kw: None)
    monkeypatch.setattr(preflight, "turned_off", lambda *a: None)
    monkeypatch.setattr(provider_homes, "decide", lambda *a: "my-provider-home")
    if provider == "codex":
        monkeypatch.setattr(providers, "_codex_layers", lambda cwd: [])
        monkeypatch.setattr(codex_hook, "hook_command", lambda: "brindle _hook codex-permission-request")
        monkeypatch.setattr(codex_hook, "status", lambda *a: codex_hook.TRUSTED)
        monkeypatch.setattr(codex_hook, "ensure", lambda **kw: codex_hook.TRUSTED)
        monkeypatch.setattr(codex_hook, "policy_on", lambda cwd: False)
    launches = []

    def launch(db_, a, ws, **kw):
        launches.append((a, ws))
        profile = agents._profile_for(db_, a, ws)
        assert profile.permission_mode == "plan" and profile.strict_mcp and not profile.add_dirs
        if a.provider == "codex":
            import json
            import tomllib

            ctx = providers.LaunchContext(a.id, profile, None, cwd=ws.path, mode=a.mode)
            command = providers.get_provider("codex").command(ctx)
            flags = dict(arg.split("=", 1) for arg in command if arg.startswith(("default_permissions=", "permissions.")))
            name = json.loads(flags["default_permissions"])
            assert json.loads(flags[f"permissions.{name}.extends"]) == ":read-only"
            files = tomllib.loads(f"t = {flags[f'permissions.{name}.filesystem']}")["t"]
            assert files["~/.aws"] == "deny" and "write" not in files.values()
            assert codex_hook.config_flags()[1] in command and codex_hook.config_flags()[3] in command
        elif a.provider == "claude":
            ctx = providers.LaunchContext(a.id, profile, None, cwd=ws.path, mode=a.mode)
            command = providers.get_provider("claude").command(ctx)
            assert command[command.index("--tools") + 1] == ""
        elif a.provider == "antigravity":
            assert antigravity.pre_tool_decision(db_, a.id, {"toolCall": {"name": "write_to_file"}})["decision"] == "deny"
            assert antigravity.pre_tool_decision(db_, a.id, {"toolCall": {"name": "call_mcp_tool", "args": {
                "ServerName": "brindle", "ToolName": "reply_doc_question"}}})["decision"] == "allow"
        elif a.provider == "opencode":
            ctx = providers.LaunchContext(a.id, profile, None, cwd=ws.path, mode=a.mode)
            assert opencode.config(ctx, opencode.launch_for(profile, ws.path))["permission"]["*"] == "deny"
        if failed is True:
            raise RuntimeError("private credential must never reach the thread")
        db_.update_agent(a.id, headless=1)
        db_.set_status(a.id, "idle")
        if failed == "blocked":
            db_.conn.execute("INSERT INTO startup_prompts (agent_id, pane, launched_at) VALUES (?,'pane',1)", (a.id,))

    monkeypatch.setattr(agents, "_launch", launch)
    got = dq.dispatch(db, DOC, 1, **net)
    assert not messages(db)
    if provider and not failed:
        a, ws = launches[0]
        assert got["agent_id"] == a.id
        assert a.mode == "doc_question" and a.provider == provider and a.config_home == "my-provider-home"
        assert ws.path != db.get_workspace("ws1").path
        if provider == "codex":
            from dataclasses import replace
            from brindle import config

            cfg = config.load_repo_config(ws.repo_root)
            # Stock supervisor allowances and saved rules cannot grant this
            # helper shell, credential reads, edits or another MCP's tools.
            for policy in ("on", "off"):
                monkeypatch.setattr(config, "load_repo_config", lambda *args, **kwargs: replace(cfg, permission_policy=policy))
                for payload in (
                    {"tool_name": "Bash", "tool_input": {"command": "git add ."}, "cwd": ws.path},
                    {"tool_name": "Bash", "tool_input": {"command": "cat ~/.aws/credentials"}},
                    {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** Add File: x\n+x\n*** End Patch"}},
                    {"tool_name": "write_stdin", "tool_input": {"chars": "git add .\n"}},
                    {"tool_name": "mcp__other__write"}, {},
                ):
                    answer = agents.permission_request_decision(db, a, payload, provider="codex")
                    assert answer["hookSpecificOutput"]["decision"]["behavior"] == "deny"
                    assert agents.codex_pre_tool_decision(db, a, payload)["hookSpecificOutput"]["permissionDecision"] == "deny"
                reply = {"tool_name": "mcp__brindle__reply_doc_question"}
                assert agents.codex_pre_tool_decision(db, a, reply) is None
                assert agents.permission_request_decision(db, a, reply, provider="codex")["hookSpecificOutput"]["decision"]["behavior"] == "allow"

            ctx = providers.LaunchContext(a.id, agents._profile_for(db, a, ws), None, cwd=ws.path, mode=a.mode)
            with monkeypatch.context() as guard:
                guard.setattr(codex_hook, "status", lambda *a: codex_hook.REMOVED)
                with pytest.raises(codex_hook.CodexHookError):
                    providers.get_provider("codex").command(ctx)
            with monkeypatch.context() as guard:
                guard.setattr(codex_hook, "ensure", lambda **kw: codex_hook.MOVED)
                with pytest.raises(codex_hook.CodexHookError):
                    providers.get_provider("codex").command(ctx)
            with monkeypatch.context() as guard:
                guard.setattr(providers, "_codex_layers", lambda cwd: [("user", {"sandbox_mode": "workspace-write"})])
                with pytest.raises(providers.CodexSandboxRefused):
                    providers.get_provider("codex").command(ctx)
        assert dq.dispatch(db, DOC, 1, **net) == got and len(launches) == 1
        monkeypatch.setattr(mcp_server, "_calling_agent", lambda: a)
        with pytest.raises(mcp_server.ToolError):
            asyncio.run(mcp_server.mcp.call_tool("assign", {"task": "change the repo"}))
        dq.reply(db, db.get_agent(a.id), DOC, 1, "An answer.", **net)
        assert lib.threads[0]["replies"][0]["from_supervisor"]
        db.delete_agent(a.id)
    else:
        assert got["state"] == "unavailable"
        assert bool(launches) == bool(failed)
        assert lib.threads[0]["replies"]
        assert "private credential" not in str(lib.threads)


def test_ask_the_supervisor_author_gets_immediate_answer_and_questions_are_not_feedback(db, parts, monkeypatch):
    import json
    from brindle.daemon.routes_visuals import plan_comment

    pid, lib, net = parts
    sd.share(db, pid, TEAM, **net)
    lib.author = "user_1"
    monkeypatch.setattr(ad, "_session", lambda client=None, store=None: (net["client"], net["store"]))
    original = sd._require_team
    monkeypatch.setattr(sd, "_require_team", lambda store: original(net["store"]))
    response = plan_comment(db, {"id": pid}, {"rev": 1, "anchor": {"kind": "section", "section": "summary"},
                                             "text": "Why these workers?", "question": True})
    assert not messages(db)

    async def transport():
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.body":
                entered.set()
                await release.wait()

        task = asyncio.create_task(response({"type": "http"}, None, send))
        await entered.wait()
        assert not messages(db)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not messages(db)
        # A new request whose body reaches the person releases the dispatch.
        release.set()
        await response({"type": "http"}, None, send)

    asyncio.run(transport())
    got = json.loads(response.body)
    assert messages(db) and got["threads"][0]["question"]
    from brindle.pro import doc_questions as dq
    dq.reply(db, db.get_agent("boss"), DOC, lib.threads[0]["id"], "An answer.", **net)
    lib.threads.append({"id": 99, "kind": "comment", "rev": 1, "author": "teammate", "question": True,
                        "text": "Teammate private question", "status": "open", "replies": [],
                        "anchor": {"kind": "section", "section": "summary"}})
    lib.reviewers = [{"sub": "teammate", "required": True}]
    lib.sign_offs = [{"sub": "teammate", "rev": 1, "at": 1.0}]
    before = messages(db)
    sd.sync(db, pid, **net)
    assert messages(db) == before
    assert plan_docs.pending(db, plans.get(db, pid)) == ([], [], [])
    threads = plans.to_dto(db, plans.get(db, pid))["threads"]
    assert threads[0]["replies"][0]["from_supervisor"]
    with pytest.raises(ServiceError):
        plans.reply(db, db.get_agent("boss"), pid, threads[-1]["id"], "Not mine to answer.")
    sd.sign_off(db, pid, 1, **net)
    assert plans.to_dto(db, plans.get(db, pid))["gate"]["state"] == "ready"


def test_ask_the_supervisor_on_a_local_doc(db, parts):
    from brindle.pro import doc_questions as dq

    pid, _, _ = parts
    tid = plan_docs.comment(db, pid, 1, {"kind": "section", "section": "summary"}, "Why?", question=True)
    dq.ask_plan(db, pid, tid)
    assert db.pending_count("boss") == 1
    before = messages(db)
    plans.reply(db, db.get_agent("boss"), pid, tid, "To keep execution local.")
    assert plans.to_dto(db, plans.get(db, pid))["threads"][0]["replies"][0]["from_supervisor"]
    assert plan_docs.pending(db, plans.get(db, pid)) == ([], [], [])
    db.set_status("boss", "paused")
    other = plan_docs.comment(db, pid, 1, {"kind": "section", "section": "summary"}, "More?", question=True)
    dq.ask_plan(db, pid, other)
    assert messages(db) == before
    assert plans.to_dto(db, plans.get(db, pid))["threads"][-1]["replies"][0]["author"] == "system"


def test_required_reviewers_share_sign_off_and_build_through_the_daemon_routes(db, parts, monkeypatch):
    """Sharing, editing reviewers, a teammate's sign-off and the build, as the app drives them."""
    pid, lib, net = parts
    monkeypatch.setattr(ad, "_session", lambda client=None, store=None: (net["client"], net["store"]))
    uploads = []
    monkeypatch.setattr(ad, "on_merged", lambda *a: uploads.append(a))
    with monkeypatch.context() as clock:
        clock.setattr(plan_docs.time, "time", lambda: 1000.0)
        plan_docs.comment(db, pid, 1, {"kind": "section", "section": "summary"}, "Private drafting note.")
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        post = lambda tail, **body: app.post(f"/v1/plans/{pid}/{tail}", json=body)  # noqa: E731
        got = post("share", scope=TEAM, reviewers=[{"sub": "u_a", "required": True}, {"sub": "user_1", "required": True}])
        assert got.status_code == 200, got.json()
        assert (got.json()["gate"]["state"], got.json()["gate"]["required"]) == ("waiting", 2)
        assert post("decide", rev=1, approved=True).json()["error"]["code"] == "gate_closed"

        # The owner signs from the doc; a teammate signs in the Library and the sync brings it back.
        assert post("sign-off", rev=1).json()["gate"]["signed"] == 1
        lib.sign_offs.append({"rev": 1, "sub": "u_a", "at": 9.0})
        assert post("sync").json()["gate"]["state"] == "ready"
        from brindle.pro import shared_reader

        assert lib.threads == []  # the pre-sharing comment stays private, outside the shared review
        assert shared_reader.read(DOC, **net)["gate"]["state"] == "ready"

        # The sharing account is also required. Its later local objection stays
        # authored as "person" after sync, but must revoke its earlier sign-off.
        lib.sign_offs[0]["at"] = 1.0
        with monkeypatch.context() as clock:
            clock.setattr(plan_docs.time, "time", lambda: 1000.0)  # the local clock differs from the Library's
            plan_docs.comment(db, pid, 1, {"kind": "section", "section": "summary"}, "Rework the split.")
        assert plans.gate(db, plans.get(db, pid))["state"] == "changes_requested"  # not sent yet
        lib.lose_answer = True
        with pytest.raises(auth.AuthError):
            sd.sync(db, pid, **net)
        assert plans.gate(db, plans.get(db, pid))["state"] == "changes_requested"  # send outcome unknown
        lib.lose_answer = False
        assert post("sync").json()["gate"]["state"] == "changes_requested"

        assert shared_reader.read(DOC, **net)["gate"]["state"] == "changes_requested"
        refused = post("decide", rev=1, approved=True)
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "gate_closed"
        assert plans.get(db, pid)["state"] == "open"
        # Signing again after the comment clears the objection on both views,
        # comparing Library times rather than the local clock's comment time.
        assert post("sign-off", rev=1).json()["gate"]["state"] == "ready"
        assert shared_reader.read(DOC, **net)["gate"]["state"] == "ready"

        # A required reviewer added in the Library since the last sync: the approval finds out.
        lib.reviewers = [*lib.reviewers, {"sub": "u_b", "required": True}]
        assert post("decide", rev=1, approved=True).status_code == 409
        assert sd.sync_open(db, **net) == 1
        assert plans.get(db, pid)["state"] == "open"

        assert post("reviewers", reviewers=[{"sub": "u_a", "required": True},
                                            {"sub": "user_1", "required": True}]).json()["gate"]["required"] == 2
        assert post("decide", rev=1, approved=True).json()["state"] == "approved"
        assert not uploads                                          # its one Library entry is the shared document
        assert post("reviewers", reviewers=[]).json()["error"]["code"] == "review_closed"
        assert sd.sync_open(db, **net) == 0                         # review closed: nothing more to bring back


def test_required_reviewers_are_synced_in_the_background(monkeypatch):
    from brindle.daemon import lifecycle
    from brindle.daemon.session_review import ReadinessWatch

    now, synced = [0.0], []

    class Server:
        should_exit = False

    async def elapse(seconds):
        now[0] += lifecycle.SHARED_DOCS_EVERY + 1
        await asyncio.to_thread(int)            # lets the sync's own task run
        if synced:
            Server.should_exit = True

    monkeypatch.setattr(lifecycle, "_sync_shared_docs", lambda: synced.append(True))
    monkeypatch.setattr(ReadinessWatch, "sweep_safely", lambda self: None)
    monkeypatch.setattr(lifecycle, "_purge_typed_words", lambda: None)
    monkeypatch.setattr(lifecycle, "_keep_codex_hook", lambda settle=0.0: None)
    asyncio.run(lifecycle._watch(Server(), lifecycle.Activity(build_app(TOKEN, port=8123)), False, wait=elapse,
                                 clock=lambda: now[0]))
    assert synced


def test_a_teammate_with_no_local_plan_gets_the_review_requested_card_from_the_library_queue(
        db, parts, backend, monkeypatch):
    """The card comes from the Library's queue (no plan here), clears when the item leaves it, and a
    failed poll keeps it without an error."""
    from brindle.daemon import lifecycle
    from brindle.pro import review_queue as rq
    from brindle.service import inbox

    pid, lib, net = parts
    queue = {"status": 200, "requests": [{"doc_id": "ad_9", "title": "Cloud workers for Pro", "rev": 3}]}
    from brindle.pro import license
    from pro_fixtures import claims, sign

    store, alice = net["store"], net["store"].load()
    bob = {**alice, "access_token": "bob_access", "refresh_token": "bob_refresh",
           "entitlement": sign(backend.key, claims(sub="u_bob", features=list(TEAM_LOGIN)))}
    signed_in = lambda: license.verify(store.load()["entitlement"], issuer=None, grace=0).sub  # noqa: E731
    during = []                                              # what happens to the stored sign-in mid-request

    def answer(form, headers):
        for record in during:
            store.save(record)
        if headers.get("Authorization") != f"Bearer {alice['access_token']}":
            return 200, {"requests": [{"doc_id": "ad_bob", "title": "Bob's private design"}]}
        return queue["status"], dict(queue)
    backend.routes["GET /me/analysis-docs/review-requests"] = answer
    monkeypatch.setattr(inbox, "_my_sub", signed_in)
    monkeypatch.setattr(ad, "_session", lambda client=None, store=None: (net["client"], net["store"]))
    rq.clear()
    cards = lambda: [(i.kind, i.doc, i.detail) for i in inbox._review_items(db, None)]  # noqa: E731
    assert cards() == []                                     # nothing polled yet
    lifecycle._poll_review_queue()
    assert cards() == [("review_requested", "ad_9", "Cloud workers for Pro")]
    queue["status"] = 503
    lifecycle._poll_review_queue()
    assert len(cards()) == 1                                 # a failed poll keeps the card
    queue.update(status=200, requests=[])
    lifecycle._poll_review_queue()
    assert cards() == []                                     # it left the queue

    # A doc this machine has a plan for follows the queue too, and the plan only adds the session link.
    sd.share(db, pid, TEAM, **net)
    queue["requests"] = [{"doc_id": DOC, "title": "Remote title"}]
    lifecycle._poll_review_queue()
    assert [(i.kind, i.doc, i.request_id) for i in inbox._review_items(db, None)] == [("review_requested", DOC, pid)]
    queue["requests"] = []
    lifecycle._poll_review_queue()
    assert cards() == []

    # The stored sign-in changes while the request runs (Alice -> Bob -> Alice, or ending as Bob): the request
    # still carries Alice's own token and the answer is filed under Alice, never under whoever is signed in after.
    queue["requests"] = [{"doc_id": "ad_alice", "title": "Alice's"}]
    for switches, end in (([bob, alice], "user_1"), ([bob], "u_bob")):
        rq.clear()
        during[:] = switches
        store.save(alice)
        lifecycle._poll_review_queue()
        assert [q["doc_id"] for q in rq.waiting("user_1")] == ["ad_alice"]
        assert rq.waiting("u_bob") == [] and signed_in() == end
    during.clear()

    # An expired token is refreshed from the captured record itself, and the request carries the new token.
    store.save({**alice, "access_expires_at": 0})
    backend.routes["GET /entitlement"] = lambda f, h: (200, {
        "entitlement": sign(backend.key, claims(features=list(TEAM_LOGIN))), "plan": "pro", "status": "active",
        "features": list(TEAM_LOGIN), "seats": 5, "expires_at": int(time.time()) + 3600})
    queue["requests"] = [{"doc_id": "ad_fresh", "title": "After refresh"}]
    seen_token = []
    backend.routes["GET /me/analysis-docs/review-requests"] = lambda f, h: (
        seen_token.append(h["Authorization"]), (200, dict(queue)))[1]
    lifecycle._poll_review_queue()
    assert cards() == [("review_requested", "ad_fresh", "After refresh")]
    assert seen_token == [f"Bearer {store.load()['access_token']}"] != [f"Bearer {alice['access_token']}"]


def test_a_shared_plan_goes_to_the_library_and_what_teammates_write_comes_back(db, parts, backend):
    pid, lib, net = parts
    grant = {"u_c": {"can_edit": False, "can_suggest": False, "can_comment": True}}
    sd.share(db, pid, TEAM, reviewers=[{"sub": "u_a"}, {"sub": "u_b", "required": False}], permissions=grant, **net)
    [made] = sent(backend, "POST /analysis-docs")
    assert made["scope"] == {"kind": "user", "id": "user_1"} and "folder" not in made       # the default Designs folder
    assert made["design"]["sections"][0]["markdown"] == SUMMARY
    assert sent(backend, f"POST {BASE}/share")[0]["scope"] == TEAM
    assert lib.reviewers == [{"sub": "u_a", "required": True}, {"sub": "u_b", "required": False}]
    assert lib.permissions["u_a"]["can_comment"] and lib.permissions["u_c"] == grant["u_c"]
    assert sd.sync(db, pid, **net)["pushed"] is False and not sent(backend, f"POST {DESIGN}/revisions")

    # What the Library answers: anchors located, an insertion point's context left out; one row this can't read.
    lib.threads = [
        {"id": 1, "kind": "comment", "rev": 1, "author": "u_a", "text": "Does a rented VM count?", "at": 5.0,
         "status": "open", "replies": [{"id": 2, "author": "u_b", "text": "Yes.", "at": 6.0}],
         "anchor": {"kind": "text", "section": "summary", "start": 0, "end": 7, "quote": "Workers", "state": "current"}},
        {"id": 3, "kind": "suggestion", "rev": 1, "author": "u_b", "text": "", "after": "Today, ", "before": "",
         "status": "open", "replies": [],
         "anchor": {"kind": "text", "section": "summary", "start": 0, "end": 0, "quote": "", "state": "current"}},
        {"id": 4, "kind": "comment", "rev": 1, "author": "u_a", "text": "?", "status": "open", "replies": [],
         "anchor": {"kind": "elsewhere"}}]
    lib.votes = {"d1": {"repo": ["u_c"]}}
    assert sd.sync(db, pid, **net)["pulled"] > 0
    assert sd.sync(db, pid, **net)["pulled"] == 0                  # once, however often it runs

    got = plan_docs.fields(db, plans.get(db, pid))
    comment, suggestion = got["threads"]
    assert (comment["author"], comment["anchor"]["state"]) == ("teammate:u_a", "current")
    assert [r["author"] for r in comment["replies"]] == ["teammate:u_b"]
    assert (got["decisions"][0]["votes"], got["decisions"][0]["confirmed"]) == ({"repo": ["teammate:u_c"]}, None)
    assert sd.link(db, pid)["review"]["pending_reviewers"] == ["u_a"]

    lib.threads[0]["status"] = "resolved"
    sd.sync(db, pid, **net)
    assert plan_docs.fields(db, plans.get(db, pid))["threads"][0]["status"] == "resolved"
    lib.threads[0]["status"] = "open"
    sd.sync(db, pid, **net)

    # The person takes a teammate's insertion: the Library's own accept makes the revision and closes
    # the thread, and in that one sync the old revision's imported votes go, the person's own staying.
    plan_docs.confirm(db, pid, 1, "d1", "org")
    plan_docs.accept(db, pid, suggestion["id"], 1)
    assert sd.sync(db, pid, **net)["pushed"] is True
    assert sent(backend, f"POST {DESIGN}/threads/3") == [{"rev": 1, "action": "accept"}]
    assert not sent(backend, f"POST {DESIGN}/revisions")            # a second send would apply it twice
    assert lib.content["sections"][0]["markdown"] == "Today, " + SUMMARY and lib.threads[1]["status"] == "accepted"
    assert (sd.link(db, pid)["remote_rev"], sd.link(db, pid)["local_rev"]) == (2, 2)
    assert plan_docs.fields(db, plans.get(db, pid))["decisions"][0]["votes"] == {"org": ["person"]}

    # Both changed: nothing is sent, the screen can say why, and the comments still arrive.
    lib.rev += 1
    lib.threads.append({"id": 5, "kind": "comment", "rev": 3, "author": "u_a", "text": "Name the VM rule.",
                        "status": "open", "replies": [], "anchor": {"kind": "section", "section": "summary"}})
    plan_docs.edit(db, pid, 2, "summary", 0, "Today", "Now")
    assert sd.sync(db, pid, **net)["pushed"] is False
    assert not sent(backend, f"POST {DESIGN}/revisions") and sd.link(db, pid)["local_rev"] == 2
    assert plans.to_dto(db, plans.get(db, pid))["gate"]["state"] == "conflict"
    assert plans.gate_shut(db, plans.get(db, pid)["root_id"])
    assert plan_docs.fields(db, plans.get(db, pid))["threads"][-1]["text"] == "Name the VM rule."

    # Request changes carries teammates' open comments and replies to the supervisor.
    plans.decide(db, pid, 3, False, person=True)
    said = messages(db)[-1]
    assert "Name the VM rule." in said and "u_a" in said and "Yes." in said and "u_b" in said


class Relay:
    """The relay a live draft's Loro updates travel on, over the Library's draft."""

    def __init__(self, lib):
        self.lib = lib

    def pull(self, doc_id, since):
        return self.lib.draft.export_updates(since), self.lib.draft.version()

    def push(self, doc_id, updates):
        self.lib.draft.import_updates(updates)


def test_two_writers_in_one_shared_section_both_land_in_the_live_draft_and_only_versions_count(
        db, parts, backend, monkeypatch):
    pid, lib, net = parts
    sd.share(db, pid, TEAM, reviewers=[{"sub": "u_a"}], **net)
    lib.sign_offs.append({"rev": 1, "sub": "u_a", "at": 9.0})
    lib.open_draft()
    monkeypatch.setattr(sd, "relays", {DOC: Relay(lib)}, raising=False)
    sd.sync(db, pid, **net)
    dto = lambda: plans.to_dto(db, plans.get(db, pid))  # noqa: E731

    # A teammate adds a sentence while the person rewrites the first one, neither seeing the other's.
    lib.draft.insert("summary", len(SUMMARY), " It stays on.")
    plan_docs.edit(db, pid, 1, "summary", 0, "Workers", "Hosts")
    sd.sync(db, pid, **net)
    both = "Hosts run on a machine the customer controls. It stays on."
    assert lib.draft.text("summary") == dto()["sections"][0]["markdown"] == both
    # No revision is made or sent, so r1's sign-off still counts and nothing is in conflict.
    assert not sent(backend, f"POST {DESIGN}/revisions")
    assert (dto()["rev"], dto()["gate"]["state"]) == (1, "ready")

    # A comment on words only the draft has reaches the Library.
    plan_docs.comment(db, pid, 1, {"kind": "text", "section": "summary", "start": both.index("It"),
                                   "quote": "It stays"}, "Says who?")
    # A suggestion on the last "on" follows its words as a teammate types before them, and is
    # accepted before the Library has it: it goes up, then is marked accepted, its text applied once.
    plan_docs.suggest(db, pid, 1, "summary", both.rindex("on"), "on", "put")
    lib.draft.insert("summary", 0, "The team agreed, after a call: ")
    # A teammate suggests an insertion in words only the draft has: its cursor alone places it.
    stays = lib.draft.text("summary").rindex(" on")
    lib.threads.append({"id": 1, "kind": "suggestion", "rev": 1, "author": "u_b", "text": "", "after": " always",
                        "status": "open", "replies": [], "at": 9.0, "anchor": {
                            "kind": "text", "section": "summary", "start": stays, "end": stays, "quote": "",
                            "state": "current",
                            "cursor": base64.b64encode(lib.draft.cursor("summary", stays)).decode()}})
    lib.refuse_suggest = True
    sd.sync(db, pid, **net)
    for who in ("person", "teammate:u_b"):
        plan_docs.accept(db, pid, next(t["id"] for t in dto()["threads"]
                                       if t["kind"] == "suggestion" and t["author"] == who), 1)
    lib.refuse_suggest = False
    sd.sync(db, pid, **net)
    agreed = "The team agreed, after a call: Hosts run on a machine the customer controls. It stays always put."
    assert lib.draft.text("summary") == dto()["sections"][0]["markdown"] == agreed
    assert dto()["rev"] == 1
    assert [(t["kind"], t["anchor"]["quote"], t["status"]) for t in lib.threads] == [
        ("suggestion", "", "accepted"), ("comment", "It stays", "open"), ("suggestion", "on", "accepted")]

    # Only a version frozen in the Library counts: it comes in, and r1's sign-off isn't on it.
    lib.freeze()
    sd.sync(db, pid, **net)
    got = dto()
    assert (got["rev"], got["sections"][0]["markdown"], got["gate"]["state"]) == (2, agreed, "waiting")


def test_an_edit_made_here_before_the_live_draft_arrives_is_carried_into_it_as_a_suggestion(
        db, parts, backend, monkeypatch):
    pid, lib, net = parts
    sd.share(db, pid, TEAM, **net)
    plan_docs.edit(db, pid, 1, "summary", 0, "Workers", "Hosts")         # r2, not sent yet
    lib.rev, lib.author = 2, "u_b"                                       # meanwhile a teammate's revision,
    lib.content["sections"][0]["markdown"] = SUMMARY + " It stays on."
    lib.open_draft()                                                     # then the draft opens on it
    monkeypatch.setattr(sd, "relays", {DOC: Relay(lib)}, raising=False)
    sd.sync(db, pid, **net)

    got = plans.to_dto(db, plans.get(db, pid))
    assert (got["sections"][0]["markdown"], got["gate"]["state"]) == (SUMMARY + " It stays on.", "ready")
    [carried] = got["threads"]
    assert (carried["kind"], carried["before"], carried["after"], carried["author"]) == (
        "suggestion", "Workers", "Hosts", "person")
    # It goes up; a teammate then types the same word before it, and it follows its own.
    lib.draft.insert("summary", 0, "Workers coordinate. ")
    sd.sync(db, pid, **net)
    # Accepted while the relay is away, it is marked accepted only once the relay has its text.
    relay = sd.relays.pop(DOC)
    plan_docs.accept(db, pid, carried["id"], got["rev"])
    sd.sync(db, pid, **net)
    assert [t["status"] for t in lib.threads] == ["open"] and "Hosts" not in lib.draft.text("summary")
    sd.relays[DOC] = relay
    sd.sync(db, pid, **net)
    assert lib.draft.text("summary") == "Workers coordinate. Hosts run on a machine the customer controls. It stays on."
    assert [t["status"] for t in lib.threads] == ["accepted"] and not sent(backend, f"POST {DESIGN}/revisions")


def test_a_teammates_edit_comes_in_as_a_revision_and_later_sends_go_on_it(db, parts, backend):
    pid, lib, net = parts
    sd.share(db, pid, TEAM, **net)
    lib.rev = 2
    lib.content["sections"][0]["markdown"] = "Workers run anywhere."
    assert sd.sync(db, pid, **net)["pushed"] is False
    got = plans.to_dto(db, plans.get(db, pid))
    assert (got["rev"], got["sections"][0]["markdown"]) == (2, "Workers run anywhere.")
    assert got["revisions"][-1]["author"] == "teammate:u_e"
    assert (sd.link(db, pid)["remote_rev"], sd.link(db, pid)["local_rev"]) == (2, 2)

    # The push that would have been left blocked goes up on the imported head.
    plan_docs.edit(db, pid, 2, "summary", 0, "Workers", "Hosts")
    assert sd.sync(db, pid, **net)["pushed"] is True
    assert [p["rev"] for p in sent(backend, f"POST {DESIGN}/revisions")] == [2]

    # A head that couldn't have been proposed here (it holds a script) isn't taken.
    lib.rev += 1
    lib.content["sections"][0].update(kind="svg", markdown="<svg><script>x</script></svg>")
    with pytest.raises(RuntimeError):
        sd.sync(db, pid, **net)
    assert plans.get(db, pid)["rev"] == 3

    # A redirected plan is the supervisor's to revise, who hears about the teammate's edit.
    lib.content["sections"][0].update(kind="markdown", markdown="Back to text.")
    plans.decide(db, pid, 3, False, note="Tighten it.", person=True)
    assert "u_e" in messages(db)[-1]
    sd.sync(db, pid, **net)
    assert plans.get(db, pid)["rev"] == 3


def test_what_the_person_does_on_the_doc_reaches_the_library_once(db, parts, backend):
    pid, lib, net = parts
    plan_docs.comment(db, pid, 1, {"kind": "section", "section": "summary"}, "Just for me.")
    sd.share(db, pid, TEAM, **net)
    lib.threads = [
        {"id": 1, "kind": "comment", "rev": 1, "author": "u_a", "text": "Which VM?", "status": "open", "replies": [],
         "anchor": {"kind": "section", "section": "summary"}},
        {"id": 2, "kind": "suggestion", "rev": 1, "author": "u_b", "text": "", "after": "Agents", "status": "open",
         "replies": [],
         "anchor": {"kind": "text", "section": "summary", "start": 0, "end": 7, "quote": "Workers", "state": "current"}}]
    sd.sync(db, pid, **net)
    _, theirs, offer = plan_docs.fields(db, plans.get(db, pid))["threads"]

    plan_docs.comment(db, pid, 1, {"kind": "text", "section": "summary", "start": 0, "quote": "Workers"}, "Which ones?")
    plan_docs.comment(db, pid, text="Any VM.", parent=theirs["id"])
    plan_docs.suggest(db, pid, 1, "summary", 0, "Workers", "Hosts")
    plan_docs.resolve(db, pid, theirs["id"])
    plan_docs.reject(db, pid, offer["id"])
    plan_docs.confirm(db, pid, 1, "d1", "org")

    # The first write lands but its answer is lost: the next sync recognises it instead of sending it again.
    lib.lose_answer = True
    with pytest.raises(auth.AuthError):
        sd.sync(db, pid, **net)
    lib.lose_answer = False
    sd.sync(db, pid, **net)
    sd.sync(db, pid, **net)

    anchor = {"kind": "text", "section": "summary", "start": 0, "end": 7, "quote": "Workers"}
    assert sent(backend, f"POST {DESIGN}/comments") == [{"rev": 1, "anchor": anchor, "text": "Which ones?"},
                                                        {"rev": 1, "parent": 1, "text": "Any VM."}]
    assert sent(backend, f"POST {DESIGN}/suggestions") == [{"rev": 1, "section": "summary", "start": 0,
                                                            "before": "Workers", "after": "Hosts", "text": ""}]
    assert sent(backend, f"POST {DESIGN}/threads/1") == [{"rev": 1, "action": "resolve"}]
    assert sent(backend, f"POST {DESIGN}/threads/2") == [{"rev": 1, "action": "reject"}]
    assert sent(backend, f"POST {DESIGN}/votes") == [{"rev": 1, "decision": "d1", "option": "org"}]

    # What went up doesn't come back as a teammate's.
    got = plan_docs.fields(db, plans.get(db, pid))
    assert [t["author"] for t in got["threads"]] == ["person", "teammate:u_a", "teammate:u_b", "person", "person"]
    assert [r["author"] for r in got["threads"][1]["replies"]] == ["person"]
    assert got["decisions"][0]["votes"] == {"org": ["person"]}

    # A comment goes through, then an org rule refuses the vote: the comment's older answer can't clear
    # the refusal, which holds the gate shut until the Library next answers in full.
    plan_docs.comment(db, pid, 1, {"kind": "section", "section": "summary"}, "One more.")
    plan_docs.confirm(db, pid, 1, "d1", "repo")
    lib.refuse_vote = True
    sd.sync(db, pid, **net)
    assert plans.to_dto(db, plans.get(db, pid))["gate"]["state"] == "waiting"
    lib.refuse_vote = False
    sd.sync(db, pid, **net)
    assert plans.to_dto(db, plans.get(db, pid))["gate"]["state"] == "ready"


def test_an_accept_the_library_refuses_holds_the_text_until_it_goes_through(db, parts, backend):
    pid, lib, net = parts
    sd.share(db, pid, TEAM, **net)
    lib.threads = [{"id": 1, "kind": "suggestion", "rev": 1, "author": "u_b", "text": "", "after": "Agents",
                    "status": "open", "replies": [], "anchor": {"kind": "text", "section": "summary", "start": 0,
                                                                "end": 7, "quote": "Workers", "state": "current"}}]
    sd.sync(db, pid, **net)
    plan_docs.accept(db, pid, plan_docs.fields(db, plans.get(db, pid))["threads"][0]["id"], 1)

    # Refused: sending the text anyway would leave the Library's thread open for good.
    lib.refuse_accept = True
    with pytest.raises(RuntimeError):
        sd.sync(db, pid, **net)
    assert not sent(backend, f"POST {DESIGN}/revisions") and sd.link(db, pid)["local_rev"] == 1

    # Once it goes through the thread is closed and the change applied once, and it is never sent again.
    lib.refuse_accept = False
    assert sd.sync(db, pid, **net)["pushed"] is True
    assert lib.threads[0]["status"] == "accepted" and not sent(backend, f"POST {DESIGN}/revisions")
    assert lib.content["sections"][0]["markdown"] == SUMMARY.replace("Workers", "Agents", 1)
    plan_docs.edit(db, pid, 2, "summary", 0, "Agents", "Hosts")
    sd.sync(db, pid, **net)
    assert len(sent(backend, f"POST {DESIGN}/threads/1")) == 2


def test_acceptances_replay_in_order_on_the_text_each_was_made_on(db, parts, backend):
    pid, lib, net = parts
    sd.share(db, pid, TEAM, **net)
    plan_docs.accept(db, pid, plan_docs.suggest(db, pid, 1, "summary", 0, "Workers", "Hosts"), 1)
    plan_docs.accept(db, pid, plan_docs.suggest(db, pid, 2, "summary", 0, "Hosts", "Agents"), 2)

    # The suggestion can't be made yet: nothing goes past it, and the text waits.
    lib.refuse_suggest = True
    with pytest.raises(RuntimeError):
        sd.sync(db, pid, **net)
    assert lib.content["sections"][0]["markdown"] == SUMMARY and sd.link(db, pid)["local_rev"] == 1

    # Each is made on the text it changed and closed by the Library's accept; no bulk upload needed.
    lib.refuse_suggest = False
    assert sd.sync(db, pid, **net)["pushed"] is True
    assert [s["before"] for s in sent(backend, f"POST {DESIGN}/suggestions")[1:]] == ["Workers", "Hosts"]
    assert [t["status"] for t in lib.threads] == ["accepted", "accepted"]
    assert lib.content["sections"][0]["markdown"] == SUMMARY.replace("Workers", "Agents", 1)
    assert not sent(backend, f"POST {DESIGN}/revisions")

    # An edit the suggestion was written on goes up first, as its own revision.
    plan_docs.edit(db, pid, 3, "summary", 0, "Agents", "Nodes")
    plan_docs.accept(db, pid, plan_docs.suggest(db, pid, 4, "summary", 0, "Nodes", "Boxes"), 4)
    assert sd.sync(db, pid, **net)["pushed"] is True
    [edit] = sent(backend, f"POST {DESIGN}/revisions")
    assert edit["rev"] == 3 and edit["sections"][0]["markdown"].startswith("Nodes")
    assert [t["status"] for t in lib.threads] == ["accepted"] * 3
    assert lib.content["sections"][0]["markdown"] == SUMMARY.replace("Workers", "Boxes", 1)
    assert sd.link(db, pid)["local_rev"] == 5

    # That edit lands but its answer is lost, and a later revision repeats its words: recovery
    # takes the head as the edit, not as the acceptances after it, which still go up and close once.
    plan_docs.edit(db, pid, 5, "summary", 0, "Boxes", "Hosts")
    plan_docs.accept(db, pid, plan_docs.suggest(db, pid, 6, "summary", 0, "Hosts", "Agents"), 6)
    plan_docs.accept(db, pid, plan_docs.suggest(db, pid, 7, "summary", 0, "Agents", "Hosts"), 7)
    lib.lose_answer = True
    with pytest.raises(auth.AuthError):
        sd.sync(db, pid, **net)
    lib.lose_answer = False
    assert sd.sync(db, pid, **net)["pushed"] is True
    assert len(sent(backend, f"POST {DESIGN}/revisions")) == 2
    assert [t["status"] for t in lib.threads] == ["accepted"] * 5
    assert lib.content["sections"][0]["markdown"] == SUMMARY.replace("Workers", "Hosts", 1)
    assert sd.link(db, pid)["local_rev"] == 8
    assert all(len(sent(backend, f"POST {DESIGN}/threads/{n}")) == 1 for n in (4, 5))

    # An accept that lands with its answer lost is known by the thread the Library closed.
    lib.threads.append({"id": 6, "kind": "suggestion", "rev": 8, "author": "u_b", "text": "", "after": "Nodes",
                        "status": "open", "replies": [], "anchor": {"kind": "text", "section": "summary", "start": 0,
                                                                    "end": 5, "quote": "Hosts", "state": "current"}})
    sd.sync(db, pid, **net)
    plan_docs.accept(db, pid, plan_docs.fields(db, plans.get(db, pid))["threads"][-1]["id"], 8)
    lib.lose_answer = True
    with pytest.raises(auth.AuthError):
        sd.sync(db, pid, **net)
    lib.lose_answer = False
    assert sd.sync(db, pid, **net)["pushed"] is True
    assert len(sent(backend, f"POST {DESIGN}/threads/6")) == 1 and sd.link(db, pid)["local_rev"] == 9


@pytest.mark.variant("from review: a write the Library committed before a 500 was pulled as a teammate's and resent")
def test_a_comment_the_library_committed_before_a_server_error_stays_one_thread(db, parts, backend):
    pid, lib, net = parts
    sd.share(db, pid, TEAM, **net)
    mine = plan_docs.comment(db, pid, 1, {"kind": "section", "section": "summary"}, "Which ones?")
    lib.answer_status = 500
    sd.sync(db, pid, **net)
    lib.answer_status = 200
    sd.sync(db, pid, **net)
    assert len(lib.threads) == 1 and len(sent(backend, f"POST {DESIGN}/comments")) == 1
    assert [t["author"] for t in plan_docs.fields(db, plans.get(db, pid))["threads"]] == ["person"]

    plan_docs.resolve(db, pid, mine)
    sd.sync(db, pid, **net)
    assert lib.threads[0]["status"] == "resolved"


@pytest.mark.variant("from review: identical text on another anchor was taken for the sent comment and resolved it")
def test_the_same_words_elsewhere_from_this_account_are_another_thread(db, parts, backend):
    pid, lib, net = parts
    sd.share(db, pid, TEAM, **net)
    lib.threads = [{"id": 1, "kind": "comment", "rev": 1, "author": "user_1", "text": "Same words.", "status": "open",
                    "replies": [], "anchor": {"kind": "text", "section": "summary", "start": 0, "end": 7,
                                              "quote": "Workers", "state": "current"}}]
    mine = plan_docs.comment(db, pid, 1, {"kind": "section", "section": "summary"}, "Same words.")
    plan_docs.resolve(db, pid, mine)
    sd.sync(db, pid, **net)
    assert len(sent(backend, f"POST {DESIGN}/comments")) == 1 and not sent(backend, f"POST {DESIGN}/threads/1")
    assert [t["status"] for t in lib.threads] == ["open", "resolved"]


@pytest.mark.variant("from review: a thread resolved or rejected before its first sync never reached the Library")
def test_a_thread_closed_before_its_first_send_still_goes_up_then_closes(db, parts, backend):
    pid, lib, net = parts
    sd.share(db, pid, TEAM, **net)
    comment = plan_docs.comment(db, pid, 1, {"kind": "section", "section": "summary"}, "Settled already.")
    plan_docs.comment(db, pid, text="Agreed.", parent=comment)
    plan_docs.resolve(db, pid, comment)
    plan_docs.reject(db, pid, plan_docs.suggest(db, pid, 1, "summary", 8, "run", "work"))
    plan_docs.accept(db, pid, plan_docs.suggest(db, pid, 1, "summary", 0, "Workers", "Hosts"), 1)
    assert sd.sync(db, pid, **net)["pushed"] is True

    # The accepted one is placed on the text the Library holds and closes through the Library's accept.
    assert not sent(backend, f"POST {DESIGN}/revisions")
    assert lib.content["sections"][0]["markdown"] == SUMMARY.replace("Workers", "Hosts", 1)
    by_text = {t.get("after") or t["text"]: t for t in lib.threads}
    assert by_text["Settled already."]["status"] == "resolved"
    assert [r["text"] for r in by_text["Settled already."]["replies"]] == ["Agreed."]
    assert (by_text["work"]["status"], by_text["Hosts"]["status"]) == ("rejected", "accepted")


@pytest.mark.variant("other approvals run these lines but none checks the upload, so an inverted guard would pass")
def test_a_plan_that_isnt_shared_still_files_its_approval_record(db, parts, monkeypatch):
    pid, _, _ = parts
    uploads = []
    monkeypatch.setattr(ad, "on_merged", lambda *a: uploads.append(a))
    plans.decide(db, pid, 1, True, person=True)
    assert len(uploads) == 1


def _signed_in(backend, store, **over):
    t = backend.issue()
    store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                "access_expires_at": time.time() + 900, "base_url": BASE_URL,
                "entitlement": sign(backend.key, claims(**over))})
    license.clear_cache()


def _server_says(backend, key=None, **over):
    """What ``GET /entitlement`` issues from now on."""
    backend.routes["GET /entitlement"] = lambda f, h: (200, {"entitlement": sign(key or backend.key, claims(**over))})


NO_TEAM = {"features": ["analysis_docs"]}
ANOTHER_ACCOUNT = {**NO_TEAM, "sub": "user_2"}
ANOTHER_ORG = {**NO_TEAM, "org_id": "org_2"}


def _lapsed(db, pid, backend, store):
    _signed_in(backend, store, **NO_TEAM)
    _server_says(backend, **NO_TEAM)


def _no_network(db, pid, backend, store):
    _signed_in(backend, store, **NO_TEAM)
    backend.routes["GET /entitlement"] = [auth.AuthError("offline", code="network")]


def _sign_in_expired(db, pid, backend, store):
    _signed_in(backend, store, **NO_TEAM)
    backend.revoked = True


def _no_sign_in(db, pid, backend, store):
    store.delete()
    license.clear_cache()


def _unverifiable(db, pid, backend, store):
    _signed_in(backend, store, **NO_TEAM)
    _server_says(backend, key=Ed25519PrivateKey.generate(), **NO_TEAM)


def _no_entitlement(db, pid, backend, store):
    _signed_in(backend, store, **NO_TEAM)
    backend.routes["GET /entitlement"] = lambda f, h: (200, {})


def _library_down(db, pid, backend, store):
    _server_says(backend, **NO_TEAM)                # even so: only the Library's own refusal asks the question
    backend.routes[f"GET {DESIGN}"] = lambda f, h: (503, {"error": "unavailable"})


def _keys_unavailable(db, pid, backend, store):
    _server_says(backend, **NO_TEAM)
    backend.routes[f"GET {DESIGN}"] = lambda f, h: (403, {"error": "key_unavailable"})


def _other_account(db, pid, backend, store):
    _signed_in(backend, store, **ANOTHER_ACCOUNT)
    _server_says(backend, **ANOTHER_ACCOUNT)


def _other_org(db, pid, backend, store):
    _signed_in(backend, store, **ANOTHER_ORG)
    _server_says(backend, **ANOTHER_ORG)


def _no_org_on_record(db, pid, backend, store):
    db.conn.execute("UPDATE plan_shares SET org_id=NULL WHERE plan_id=?", (pid,))
    _lapsed(db, pid, backend, store)


def _bypass(arrange, why):
    return pytest.param(arrange, marks=pytest.mark.variant(f"bypass attempt: {why}"), id=arrange.__name__.strip("_"))


@pytest.mark.parametrize("arrange", [
    pytest.param(_lapsed, id="lapsed"),
    _bypass(_no_network, "a stored entitlement without Team, and no way to ask the server"),
    _bypass(_sign_in_expired, "the session is revoked, so nothing verified says the plan ended"),
    _bypass(_no_sign_in, "nobody is signed in"),
    _bypass(_unverifiable, "the server's answer carries a signature that doesn't check out"),
    _bypass(_no_entitlement, "the server answers without an entitlement"),
    _bypass(_library_down, "an outage is not a lapse"),
    _bypass(_keys_unavailable, "an org rule's 403 is not a lapse"),
    _bypass(_other_account, "switching to an account without Team must not dodge the review"),
    _bypass(_other_org, "the same person in an org without Team says nothing about the org that shared it"),
    _bypass(_no_org_on_record, "a missing identity never matches"),
])
def test_lapsed_plan_becomes_a_personal_doc(db, parts, session, backend, monkeypatch, arrange):
    pid, lib, net = parts
    boss, started = session
    monkeypatch.setattr(ad, "_session", lambda client=None, store=None: (net["client"], net["store"]))
    monkeypatch.setattr(credentials, "default_store", lambda: net["store"])
    monkeypatch.setattr(ad, "on_merged", lambda *a: None)
    sd.share(db, pid, TEAM, reviewers=[{"sub": "u_a"}, {"sub": "u_b"}], **net)
    lib.sign_offs.append({"rev": 1, "sub": "u_a", "at": 9.0})
    lib.threads = [{"id": 1, "kind": "comment", "rev": 1, "author": "u_a", "text": "Which VM?", "status": "open",
                    "at": 5.0, "replies": [], "anchor": {"kind": "section", "section": "summary"}}]
    sd.sync(db, pid, **net)
    root = plans.get(db, pid)["root_id"]
    arrange(db, pid, backend, net["store"])

    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        approve = lambda: app.post(f"/v1/plans/{pid}/decide", json={"rev": 1, "approved": True})  # noqa: E731
        if arrange is not _lapsed:
            refused = approve()
            assert refused.json()["error"]["code"] in ("gate_unverified", "gate_closed"), refused.json()
            sd.sync_open(db, **net)
            now = plans.to_dto(db, plans.get(db, pid))
            assert (now["state"], now["team_ended"]) == ("open", False) and now["gate"]["state"] != "ready"
            assert plans.gate_shut(db, root)
            return

        # The background pass finds out; nothing more is asked of the Library.
        before = len(sent(backend, f"GET {DESIGN}"))
        assert sd.sync_open(db, **net) == 0 and len(sent(backend, f"GET {DESIGN}")) == before
        got = plans.to_dto(db, plans.get(db, pid))
        assert got["gate"] is None and got["team_ended"] is True and plans.gate_shut(db, root) is None
        assert got["threads"][0]["text"] == "Which VM?"

        # Work queued behind the doc, then the app goes away: only a current confirmation may release it.
        assert "behind plan" in asyncio.run(mcp_server.assign("developer", "build it")) and started == []
        monkeypatch.setattr(holds, "app_connected", lambda: False)

        def offline():
            backend.routes["GET /entitlement"] = [auth.AuthError("offline", code="network")]

        def age(seconds, plan=pid):
            db.conn.execute("UPDATE plan_shares SET lapsed_at=? WHERE plan_id=?", (time.time() - seconds, plan))

        def released():
            assert plans.holding(db, root) is None and tasks.build_gate_refusal(db, boss.id) is None

        def held():
            assert plans.holding(db, root) and plans.gate_shut(db, root) and tasks.build_gate_refusal(db, boss.id)
            assert tasks.sweep(db) == 0 and started == []
            assert plans.to_dto(db, plans.get(db, pid))["team_ended"] is False

        def confirmed_again():
            _server_says(backend, **NO_TEAM)
            sd.sync_open(db, **net)
            released()

        released()

        # The same account's plan in another session, locked to another provider identity: the server's
        # answer is about the brindle account, so it goes for both, and the lock doesn't get in its way.
        boss2 = dataclasses.replace(boss, id="boss2", tmux_window="%2")
        db.add_agent(boss2)
        visual_offers.person_spoke(db, boss2, "Make a planning doc.")
        other = plans.propose(db, boss2, "# Other", DECISIONS, sections=[{"id": "s", "title": "S", "markdown": "T"}])["id"]
        db.conn.execute("INSERT INTO plan_shares (plan_id, doc_id, owner, org_id, remote_rev, local_rev)"
                        " VALUES (?, 'doc_2', 'user_1', 'org_1', 1, 1)", (other,))
        monkeypatch.setattr(identity_lock, "locked_by", lambda a: "another identity" if a == "boss2" else None)
        confirmed_again()
        assert sd.link(db, other)["lapsed_at"] is not None

        # A refused approval takes the confirmation back: the server has to say it again.
        offline()
        assert approve().json()["error"]["code"] == "gate_unverified"
        held()
        assert sd.link(db, other)["lapsed_at"] is None
        confirmed_again()

        # A re-check that fails takes it back too, even from a plan confirmed a moment ago.
        age(sd.LAPSE_CHECK_AFTER + 1, other)
        offline()
        sd.sync_open(db, **net)
        held()
        confirmed_again()

        # And one nobody renews runs out by itself.
        age(sd.LAPSE_GOOD_FOR + 1)
        held()
        confirmed_again()

        # It stands only for the account it was confirmed for.
        now = int(time.time())
        for changed in (lambda: _signed_in(backend, net["store"], **ANOTHER_ACCOUNT),
                        lambda: _signed_in(backend, net["store"], **NO_TEAM, iat=now - 7200, exp=now - 1800),
                        lambda: _no_sign_in(db, pid, backend, net["store"])):
            changed()
            sd.sync_open(db, **net)
            held()

        # Resubscribing picks the same document back up, with its review.
        _signed_in(backend, net["store"], features=list(TEAM_LOGIN))
        _server_says(backend, features=list(TEAM_LOGIN))
        assert sd.sync_open(db, **net) == 1
        back = plans.to_dto(db, plans.get(db, pid))
        assert back["gate"]["waiting_on"] == ["u_b"] and back["team_ended"] is False
        assert sd.link(db, pid)["doc_id"] == DOC and len(sent(backend, "POST /analysis-docs")) == 1

        # Lapsed again, the approval itself finds out and goes through with no reviewers to wait for.
        _lapsed(db, pid, backend, net["store"])
        assert approve().json()["state"] == "approved" and started == ["build it"]

        # Built on an approval the server confirmed: the doc keeps saying what it is, however long ago.
        age(sd.LAPSE_GOOD_FOR + 1)
        built = plans.to_dto(db, plans.get(db, pid))
        assert built["gate"] is None and built["team_ended"] is True


def test_reviewers_change_after_sharing_and_a_folder_is_only_used_when_named(db, parts, backend):
    pid, lib, net = parts
    folder = {"scope": TEAM, "id": "f_1"}
    sd.share(db, pid, TEAM, reviewers=[{"sub": "u_a"}], folder=folder, **net)
    assert sent(backend, f"PUT {DESIGN}/folder") == [{"folder": folder}]
    lib.permissions["u_x"] = {"can_edit": True, "can_suggest": True, "can_comment": True}

    sd.set_reviewers(db, pid, [{"sub": "u_b", "required": False}], **net)
    assert lib.reviewers == [{"sub": "u_b", "required": False}]
    assert lib.permissions["u_x"]["can_edit"] and lib.permissions["u_b"]["can_comment"]   # added to, not replaced
    assert sd.link(db, pid)["review"]["ready"] is True
    with pytest.raises(RuntimeError):
        sd.set_reviewers(db, pid, [{"required": True}], **net)
    assert lib.reviewers == [{"sub": "u_b", "required": False}]


def test_an_interrupted_share_or_send_keeps_one_document_and_one_revision(db, parts, backend):
    pid, lib, net = parts
    # A refused create leaves no claim behind.
    lib.create_status = 403
    with pytest.raises(RuntimeError, match="switch_off"):
        sd.share(db, pid, TEAM, **net)
    assert sd.link(db, pid) is None
    lib.create_status = 200

    # A claim that died before the Library got anything: nothing to find, so it creates.
    now = time.time()
    db.conn.execute("INSERT INTO plan_shares (plan_id, owner, claimed_at) VALUES (?, 'user_1', ?)", (pid, now))
    assert sd.share(db, pid, TEAM, now=now + sd.CLAIM_FOR + 1, **net)["doc_id"] == DOC
    assert len(sent(backend, "POST /analysis-docs")) == 2
    db.conn.execute("DELETE FROM plan_shares WHERE plan_id=?", (pid,))
    lib.plan = None

    # A create that commits but whose answer is lost: the outcome is kept, and a second share leaves it alone.
    lib.lose_create = True
    with pytest.raises(auth.AuthError):
        sd.share(db, pid, TEAM, now=now, **net)
    lib.lose_create = False
    assert sd.link(db, pid)["doc_id"] is None
    with pytest.raises(RuntimeError):
        sd.share(db, pid, TEAM, now=now + 1, **net)

    # When the Library can't say whether it landed, nothing is created.
    lib.list_status = 500
    with pytest.raises(RuntimeError):
        sd.share(db, pid, TEAM, now=now + sd.CLAIM_FOR + 1, **net)
    lib.list_status = 200
    assert len(sent(backend, "POST /analysis-docs")) == 3 and sd.link(db, pid)["doc_id"] is None

    # A later share finds that document; one that fails after it is known finishes on the same document.
    lib.share_status = 500
    with pytest.raises(RuntimeError, match="too_many_scopes"):
        sd.share(db, pid, TEAM, now=now + 2 * sd.CLAIM_FOR + 2, **net)
    lib.share_status = 200
    assert sd.share(db, pid, TEAM, **net)["doc_id"] == DOC
    assert len(sent(backend, "POST /analysis-docs")) == 3 and len(sent(backend, f"POST {BASE}/share")) == 3

    # A send in flight is left alone.
    plan_docs.edit(db, pid, 1, "summary", 0, "Workers", "Hosts")
    db.conn.execute("UPDATE plan_shares SET claimed_at=? WHERE plan_id=?", (now, pid))
    assert sd.sync(db, pid, now=now + 1, **net)["pushed"] is False
    assert not sent(backend, f"POST {DESIGN}/revisions")
    db.conn.execute("UPDATE plan_shares SET claimed_at=NULL WHERE plan_id=?", (pid,))

    # A send whose answer is lost isn't sent twice: the next sync sees it landed.
    lib.lose_answer = True
    with pytest.raises(auth.AuthError):
        sd.sync(db, pid, **net)
    lib.lose_answer = False
    assert sd.sync(db, pid, **net)["pushed"] is True
    assert len(sent(backend, f"POST {DESIGN}/revisions")) == 1 and sd.link(db, pid)["remote_rev"] == 2


def test_an_insertion_the_library_cant_place_here_stays_detached(db, parts):
    pid, lib, net = parts
    sd.share(db, pid, TEAM, **net)

    def insertion(tid, state):
        return {"id": tid, "kind": "suggestion", "rev": 1, "author": "u_b", "text": "", "after": "Today, ",
                "status": "open", "replies": [],
                "anchor": {"kind": "text", "section": "summary", "start": 0, "end": 0, "quote": "", "state": state}}

    def refused_last():
        thread = plan_docs.fields(db, plans.get(db, pid))["threads"][-1]
        assert thread["anchor"]["state"] == "detached"
        with pytest.raises(ServiceError) as refused:
            plan_docs.accept(db, pid, thread["id"], plans.get(db, pid)["rev"])
        assert refused.value.code == "stale"

    lib.threads = [insertion(1, "detached")]                # the Library says it is gone
    sd.sync(db, pid, **net)
    refused_last()
    plan_docs.edit(db, pid, 1, "summary", 0, "Workers", "Hosts")
    lib.rev += 1                                            # placed, but on text this plan doesn't have
    lib.threads.append(insertion(2, "current"))
    sd.sync(db, pid, **net)
    refused_last()


def test_nothing_is_sent_without_team_or_from_another_account(db, parts, backend):
    pid, lib, net = parts
    login_as(backend, net["store"], features=("analysis_docs",))
    with pytest.raises(RuntimeError):
        sd.share(db, pid, TEAM, **net)
    login_as(backend, net["store"], features=TEAM_LOGIN)
    with pytest.raises(RuntimeError):
        sd.share(db, pid, {"kind": "user", "id": "u_a"}, **net)
    assert not sent(backend, "POST /analysis-docs") and sd.link(db, pid) is None

    sd.share(db, pid, TEAM, **net)
    db.conn.execute("UPDATE plan_shares SET owner='someone_else' WHERE plan_id=?", (pid,))
    with pytest.raises(RuntimeError):
        sd.sync(db, pid, **net)
    assert not sent(backend, f"GET {DESIGN}")


def test_a_plan_with_no_sections_is_refused_before_anything_is_created(db, session, tmp_path, backend):
    boss, _ = session
    store = credentials.FileStore(tmp_path / "pro")
    login_as(backend, store, features=TEAM_LOGIN)
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = plans.propose(db, boss, "# Just a title\n\nNo headings.")["id"]
    Library(backend)
    with pytest.raises(RuntimeError):
        sd.share(db, pid, TEAM, client=auth.Client(None, backend), store=store)
    assert not sent(backend, "POST /analysis-docs") and sd.link(db, pid) is None
