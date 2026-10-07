"""Rule packs checked against a branch's diff, and the merge gate they feed."""
import json
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import gates, rule_checks, workspaces
from brindle.config import load_repo_config
from brindle.db import Agent
from brindle.profiles import RulePack

DIFF = """\
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 import os
-import json
+import pickle
+import subprocess
 x = 1
@@ -10,2 +12,3 @@ def f():
     y = 2
+    subprocess.run(cmd, shell=True)
     return y
diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -5,1 +5,2 @@
 dependencies = [
+  "requests>=2",
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1,1 +0,0 @@
-gone = True
diff --git a/img.png b/img.png
Binary files differ
"""


def test_parse_diff_tracks_added_lines_and_touched_files():
    d = rule_checks.parse_diff(DIFF)
    assert d.files == ["src/app.py", "pyproject.toml", "old.py"]
    assert d.added["src/app.py"] == [(2, "import pickle"), (3, "import subprocess"),
                                     (13, "    subprocess.run(cmd, shell=True)")]
    assert d.added["pyproject.toml"] == [(6, '  "requests>=2",')]
    assert "old.py" not in d.added


def test_parse_diff_is_not_fooled_by_content_that_looks_like_headers():
    """A removed ``-- sql comment`` renders as ``--- sql comment`` and an added
    ``++ x`` as ``+++ x``: hunk line counts say they're content, so a worker
    can't credit its lines to a test file (or any file) of its choosing."""
    text = (
        "diff --git a/src/q.sql b/src/q.sql\n--- a/src/q.sql\n+++ b/src/q.sql\n"
        "@@ -1,2 +1,5 @@\n"
        "--- old comment\n"
        "+++ tests/test_fake.py\n"
        "+diff --git a/tests/t.py b/tests/t.py\n"
        "+\n"
        " keep\n"
        "+shell=True\n"
        "diff --git a/README b/README\n--- a/README\n+++ b/README\n"
        "@@ -1 +1 @@\n-a\n+b\n"
    )
    d = rule_checks.parse_diff(text)
    assert d.files == ["src/q.sql", "README"]
    assert d.added["src/q.sql"] == [(1, "++ tests/test_fake.py"), (2, "diff --git a/tests/t.py b/tests/t.py"),
                                    (3, ""), (5, "shell=True")]
    assert d.added["README"] == [(1, "b")]
    assert rule_checks.check_require_tests(pack(require_tests_for=["src/*"]), d)


def test_parse_diff_handles_blank_context_and_quoted_paths():
    text = (
        'diff --git "a/src/caf\\303\\251 x.py" "b/src/caf\\303\\251 x.py"\n'
        '--- "a/src/caf\\303\\251 x.py"\n+++ "b/src/caf\\303\\251 x.py"\n'
        "@@ -1,3 +1,4 @@\n a\n\n+new\n b\n"
        "diff --git a/one.py b/one.py\n--- a/one.py\n+++ b/one.py\n@@ -1 +1,2 @@\n x\n+y\n"
    )
    d = rule_checks.parse_diff(text)
    assert d.files == ["src/café x.py", "one.py"]
    assert d.added["src/café x.py"] == [(3, "new")]
    assert d.added["one.py"] == [(2, "y")]


def test_parse_diff_counts_lines_the_way_git_does():
    """A form feed, a bare CR or U+2028 inside a line is one line to git. If
    the parser broke there too, the hunk's counts would run out early and
    the added lines after it would never be read."""
    text = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1 +1,4 @@\n x\n+one\x0ctwo three\r\n+shell=True\n+four\x85five\n"
    )
    d = rule_checks.parse_diff(text)
    assert d.added["a.py"] == [(2, "one\x0ctwo three\r"), (3, "shell=True"), (4, "four\x85five")]
    assert rule_checks.check_deny_patterns(pack(deny_patterns=["shell=True"]), d)


def pack(**kw) -> RulePack:
    return RulePack(name=kw.pop("name", "p"), description="", prompt="", **kw)


def test_deny_deps_sees_imports_and_manifests():
    d = rule_checks.parse_diff(DIFF)
    found = rule_checks.check_deny_deps(pack(deny_deps=["pickle", "requests", "json"]), d)
    details = [v.detail for v in found]
    assert any(s.startswith("src/app.py:2: pickle imported") for s in details)
    assert any(s.startswith("pyproject.toml:6: requests added as a dependency") for s in details)
    assert not any("json" in s for s in details)   # removed, not added
    js = rule_checks.parse_diff(
        "diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -1,0 +1,3 @@\n"
        "+import lodash from 'lodash';\n+const x = require(\"left-pad\");\n+import { a } from 'lodash-es';\n")
    found = rule_checks.check_deny_deps(pack(deny_deps=["lodash", "left-pad"]), js)
    assert [v.detail.split(":")[1] for v in found] == ["1", "2"]   # lodash-es isn't lodash


def added(*lines: str, path: str = "a.py") -> rule_checks.Diff:
    return rule_checks.Diff([path], {path: [(i + 1, l) for i, l in enumerate(lines)]})


def test_deny_deps_sees_every_way_of_importing():
    hits = lambda deps, *lines, path="a.py": [   # noqa: E731
        v.detail.split(":")[1] for v in rule_checks.check_deny_deps(pack(deny_deps=deps), added(*lines, path=path))]
    assert hits(["pickle"],
                "import os, pickle",                       # 1: in a list
                "import os as o,pickle as p",              # 2
                "import pickle.sub",                       # 3
                "from pickle import loads",                # 4
                "m = importlib.import_module('pickle')",   # 5
                "m = __import__(\"pickle\")",              # 6
                "import pickles",                          # not it
                "from mypickle import x",                  # not it
                "x = pickle.loads(y)",                     # a use, not an import
                ) == ["1", "2", "3", "4", "5", "6"]
    assert hits(["pkg"],
                "export * from 'pkg'",                     # 1
                "const x = await import('pkg/sub')",       # 2
                "require 'pkg'",                           # 3: ruby
                "extern crate pkg;",                       # 4: rust
                "use ::pkg::thing;",                       # 5
                '\talias "pkg/sub"',                       # 6: go block import
                "import \"pkg\";",                         # 7
                "x = 1; import pkg",                       # 8: after a statement
                "} from 'pkg'",                            # 9: the last line of a multi-line import
                "const x = require(`pkg`)",                # 10: template quotes
                '\t. "pkg"',                               # 11: go dot import
                "pub use pkg::X;",                         # 12
                "import PKG",                              # 13: a case the regex shouldn't care about
                "import pkgs",                             # not it
                "x = fromage('pkg')",                      # not it
                ) == [str(i) for i in range(1, 14)]
    # A manifest names a package however it likes: case, '-', '_' and '.' are one.
    assert hits(["python-dateutil"], 'deps = ["Python_DateUtil>=2"]', path="pyproject.toml") == ["1"]
    assert hits(["requests"], "requests-oauthlib==1", path="requirements/dev.in") == []
    assert hits(["requests"], "requests==2", path="requirements/dev.in") == ["1"]


def test_deny_patterns_report_file_and_line_and_cap_the_noise():
    d = rule_checks.parse_diff(DIFF)
    found = rule_checks.check_deny_patterns(pack(deny_patterns=[r"shell=True", r"\bimport\s+pickle"]), d)
    assert [v.detail.split(": ")[0] for v in found] == ["src/app.py:13", "src/app.py:2"]
    assert "matches /shell=True/" in found[0].detail
    many = rule_checks.parse_diff(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,0 +1,15 @@\n" + "+TODO\n" * 15)
    found = rule_checks.check_deny_patterns(pack(deny_patterns=["TODO"]), many)
    assert len(found) == rule_checks.MAX_SHOWN_PER_RULE + 1
    assert found[-1].detail == "... and 5 more lines match /TODO/"
    bad = rule_checks.check_deny_patterns(pack(deny_patterns=["("]), d)
    assert len(bad) == 1 and "doesn't compile" in bad[0].detail


def test_require_tests_for_wants_a_test_file_in_the_diff():
    d = rule_checks.parse_diff(DIFF)
    p = pack(require_tests_for=["src/**/*.py"])
    found = rule_checks.check_require_tests(p, d)
    assert len(found) == 1 and found[0].detail.startswith("src/app.py changed, but")
    with_test = rule_checks.parse_diff(
        DIFF + "diff --git a/tests/test_app.py b/tests/test_app.py\n--- a/tests/test_app.py\n"
               "+++ b/tests/test_app.py\n@@ -1,0 +1,1 @@\n+def test(): pass\n")
    assert rule_checks.check_require_tests(p, with_test) == []
    # Deleting a test file isn't adding or changing one.
    deleted_test = rule_checks.parse_diff(
        DIFF + "diff --git a/tests/test_app.py b/tests/test_app.py\ndeleted file mode 100644\n"
               "--- a/tests/test_app.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-def test(): pass\n")
    assert "tests/test_app.py" in deleted_test.files
    assert rule_checks.check_require_tests(p, deleted_test)
    assert rule_checks.check_require_tests(pack(require_tests_for=["lib/*"]), d) == []
    # Any file at all, as tests/only has it; a diff of only tests is fine.
    only_tests = rule_checks.parse_diff(
        "diff --git a/tests/test_x.py b/tests/test_x.py\n--- a/tests/test_x.py\n+++ b/tests/test_x.py\n"
        "@@ -1,0 +1,1 @@\n+x\n")
    assert rule_checks.check_require_tests(pack(require_tests_for=["*"]), only_tests) == []
    assert rule_checks.check_require_tests(pack(require_tests_for=["*"]), d)
    for path in ("tests/x.py", "pkg/tests/x.py", "test_x.py", "a/x_test.go", "a/b.test.ts",
                 "a/b.spec.ts", "src/__tests__/b.js", "spec/a_spec.rb"):
        assert rule_checks.is_test_file(path), path
    assert not rule_checks.is_test_file("src/contest.py")
    # A differently cased path is the same path to the glob.
    cased = rule_checks.Diff(["SRC/App.py"], {"SRC/App.py": [(1, "x")]})
    assert rule_checks.check_require_tests(pack(require_tests_for=["src/**/*.py"]), cased)


def test_the_builtin_security_pack_is_not_fooled_by_spelling():
    from brindle.profiles import load_rule_pack

    sec = load_rule_pack("security/backend")
    bad = ("subprocess.run(cmd, shell = True)", "run(c, shell=1)", "Popen(c, shell=flag)",
           "os.system(cmd)", "os.popen(cmd)", "eval (expr)", "exec(code)", "pickle.load(f)",
           "yaml.load(f)", "requests.get(u, verify = False)", "requests.get(u, verify=0)",
           'API_KEY = "sk-live-0123456789abcdef"', "password: 'hunter2hunter2'")
    good = ("run(c, shell=False)", "run(c, shell=None)", "shell=0,", "yaml.load(f, Loader=SafeLoader)",
            "yaml.safe_load(f)", "evaluate(x)", "executor.submit(f)", "verify=True",
            'password = ""', "password = os.environ['PW']", "# the shell is used here")
    for line in bad:
        assert rule_checks.check_deny_patterns(sec, added(line)), line
    for line in good:
        assert not rule_checks.check_deny_patterns(sec, added(line)), line


def test_check_runs_every_pack_and_formats():
    d = rule_checks.parse_diff(DIFF)
    packs = [pack(name="a", deny_patterns=["shell=True"]), pack(name="b", deny_deps=["pickle"]),
             pack(name="c")]
    found = rule_checks.check(packs, d)
    assert [v.pack for v in found] == ["a", "b"]
    text = rule_checks.format_violations(found)
    assert text.startswith("- a (deny_patterns): src/app.py:13")
    assert "- b (deny_deps): src/app.py:2" in text


# -- against a real branch, through the gates ---------------------------------------


def add_worker(db, ws, profile, agent_id="w1"):
    db.add_agent(Agent(agent_id, ws.id, profile, "claude", "boss", "assign", "idle", "@0", None,
                       time.time()))


@pytest.fixture
def branch(db, repo):
    (repo / ".brindle" / "agents").mkdir(parents=True)
    (repo / ".brindle" / "agents" / "sec.md").write_text(
        "---\nname: sec\nextends: developer\nrules: security/backend\n---\nSec.\n")
    (repo / ".brindle" / "agents" / "tidy.md").write_text(
        "---\nname: tidy\nextends: developer\nrules: style/minimal-diff\n---\n")
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "svc.py").write_text("import subprocess\n\ndef run(cmd):\n"
                                          "    return subprocess.run(cmd, shell=True)\n")
    sh("git add svc.py && git commit -qm shell", Path(ws.path))
    return ws


def test_branch_diff_is_what_the_branch_committed(db, branch):
    d = rule_checks.branch_diff(branch.path, "main")
    assert d.files == ["svc.py"] and d.added["svc.py"][0] == (1, "import subprocess")


def test_run_checks_the_workers_packs(db, branch):
    assert rule_checks.run(db, branch) is None          # no worker: nothing to check
    add_worker(db, branch, "developer")
    assert rule_checks.run(db, branch) is None          # a worker without packs: nothing
    db.delete_agent("w1")
    add_worker(db, branch, "tidy")
    result = rule_checks.run(db, branch)
    assert result.ok and result.packs == ["style/minimal-diff"]   # prose only: passes
    assert result.summary() == "PASS `rules style/minimal-diff`"
    db.delete_agent("w1")
    add_worker(db, branch, "sec")
    result = rule_checks.run(db, branch)
    assert not result.ok and result.packs == ["security/backend"]
    [v] = result.violations
    assert v.detail.startswith("svc.py:4: matches /") and v.rule == "deny_patterns"
    assert v.detail.endswith("/: return subprocess.run(cmd, shell=True)")
    assert result.summary().startswith("FAIL `rules security/backend`\n- security/backend")
    assert gates.summary_failed(result.summary())


def test_a_profile_that_wont_load_is_a_failure_not_a_pass(db, branch, repo):
    (repo / ".brindle" / "agents" / "sec.md").write_text("---\nname: sec\nrules: no/such\n---\n")
    add_worker(db, branch, "sec")
    result = rule_checks.run(db, branch)
    assert not result.ok and "no rule pack named 'no/such'" in result.problem
    assert "couldn't run" in rule_checks.gate_problem(result, branch.branch)
    (repo / ".brindle" / "agents" / "sec.md").unlink()
    result = rule_checks.run(db, branch)        # a deleted profile fails closed, and says so
    assert not result.ok and "no agent profile named 'sec'" in result.problem
    (repo / ".brindle" / "agents" / "sec.md").write_text("---\nname: sec\nextends: sec\n---\n")
    assert "extends itself" in rule_checks.run(db, branch).problem


def test_the_branch_cannot_hide_its_lines_from_the_diff(db, branch, monkeypatch):
    """A ``-diff`` attribute committed on the branch would make git print
    "Binary files differ" instead of the lines; shared git config could
    change the prefixes or quoting the parser keys on. Neither hides a line."""
    ws_path = Path(branch.path)
    (ws_path / ".gitattributes").write_text("*.py -diff\n")
    (ws_path / "evil.py").write_text("import subprocess\nsubprocess.run(cmd, shell=True)\n")
    # A form feed and a latin-1 byte before the offending line: neither may
    # throw the line counts off or crash the read.
    (ws_path / "odd.py").write_bytes(b"# \x0c\xe9 \xe2\x80\xa8\nx = run(cmd, shell=True)\n")
    sh("git add .gitattributes evil.py odd.py && git commit -qm attrs", ws_path)
    sh("git config diff.mnemonicPrefix true && git config diff.noprefix true", ws_path)
    sh("git config core.quotepath true && git config diff.suppressBlankEmpty true", ws_path)
    d = rule_checks.branch_diff(branch.path, "main")
    assert set(d.files) == {".gitattributes", "evil.py", "odd.py", "svc.py"}
    assert (2, "subprocess.run(cmd, shell=True)") in d.added["evil.py"]
    assert (2, "x = run(cmd, shell=True)") in d.added["odd.py"]
    assert d.added["svc.py"][0] == (1, "import subprocess")
    add_worker(db, branch, "sec")
    found = rule_checks.run(db, branch).violations
    assert {v.detail.split(":")[0] for v in found} == {"evil.py", "odd.py", "svc.py"}


def test_the_merge_gate_fails_on_a_violation_and_the_summary_reports_it(db, branch, repo):
    add_worker(db, branch, "sec")
    cfg = load_repo_config(str(repo))
    report = gates.run(db, branch, cfg, review_required=False)
    assert not report.ok
    assert report.problem.startswith("Rule check failed in feat (the worker's profile rule packs):\n"
                                     "- security/backend (deny_patterns): svc.py:4")
    assert report.problem.endswith("Send this to the worker to fix.")
    summary = gates.check_summary(db, branch, cfg)     # no checks configured: the rules alone
    assert summary.startswith("FAIL `rules security/backend`") and gates.summary_failed(summary)

    (Path(branch.path) / "svc.py").write_text("import subprocess\n\ndef run(cmd):\n"
                                              "    return subprocess.run(cmd)\n")
    sh("git commit -qam fixed", Path(branch.path))
    report = gates.run(db, branch, cfg, review_required=False)
    assert report.ok and report.passed == ["rules passed (security/backend)"]
    assert gates.check_summary(db, branch, cfg) == "PASS `rules security/backend`"


def test_the_summary_lists_rules_before_the_checks(db, branch, repo, monkeypatch):
    (repo / ".brindle" / "config.json").write_text(json.dumps({"checks": ["true"]}))
    add_worker(db, branch, "sec")
    monkeypatch.setattr(gates, "run_checked", lambda db_, ws, cmd, env, timeout: (True, ""))
    summary = gates.check_summary(db, branch, load_repo_config(str(repo)))
    assert summary.startswith("FAIL `rules security/backend`") and summary.endswith("PASS `true`")
    db.delete_agent("w1")
    assert gates.check_summary(db, branch, load_repo_config(str(repo))) == "PASS `true`"
