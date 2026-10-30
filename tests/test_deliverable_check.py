"""Deliverable format and committed-branch gate dispatch."""

from types import SimpleNamespace

import pytest

from brindle import deliverable_check as checker, git, rule_checks, workspaces
from brindle.profiles import RulePack

MAIN = ".brindle/deliverables/study/main.md"
APPENDIX = ".brindle/deliverables/study/appendices/data.md"
VALID = """---
title: Study
question: What changed?
scope: user
---
## Results
1. [verified] Throughput rose 20%.
  - [not checked] Latency.
## Conclusion
1. [inference] Adopt it.
[Data](appendices/data.md)
"""


@pytest.mark.parametrize("old,new,appendices,rule", [
    ("title: Study\n", "", [APPENDIX], "title"),
    ("question: What changed?\n", "", [APPENDIX], "question"),
    ("scope: user", "scope: invalid", [APPENDIX], "scope"),
    ("title: Study", "title: [", [APPENDIX], "frontmatter"),
    ("## Results", "## Findings", [APPENDIX], "results"),
    ("20%", "substantially", [APPENDIX], "metric"),
    ("## Conclusion", "## Discussion", [APPENDIX], "conclusion"),
    ("[not checked] ", "", [APPENDIX], "tags"),
    ("", "", [], "appendices"),
    ("", "", [APPENDIX, ".brindle/deliverables/study/appendices/extra.md"], "appendix_links"),
])
def test_format_rules(old, new, appendices, rule):
    assert checker.check_main("analysis", MAIN, VALID, [APPENDIX]) == []
    failures = checker.check_main("analysis", MAIN, VALID.replace(old, new), appendices)
    assert rule in {v.rule for v in failures}


def test_gate_checks_committed_main_only_when_active(monkeypatch):
    ws = SimpleNamespace(path="/worker", repo_root="/repo", branch="worker")
    pack = RulePack("analysis", "", "", deliverables=True)
    packs = [pack]
    monkeypatch.setattr(rule_checks, "_worker_rules", lambda db, ws: (None, packs))
    monkeypatch.setattr(rule_checks, "org_protected_paths", lambda root: ((), None))
    monkeypatch.setattr(workspaces, "require_base", lambda ws: "main")
    files = [MAIN, ".brindle/deliverables/deleted/main.md", "unrelated.md"]
    monkeypatch.setattr(rule_checks, "branch_files", lambda path, base: files)
    committed = {MAIN: VALID.replace("20%", "substantially"), APPENDIX: "data"}

    def read_tree(args, cwd):
        if args[0] == "ls-tree":
            return SimpleNamespace(stdout="\0".join(committed) + "\0")
        assert args[0] == "show"
        return SimpleNamespace(stdout=committed[args[1].removeprefix("HEAD:")])

    monkeypatch.setattr(git, "run", read_tree)
    result = rule_checks.run(None, ws)
    assert not result.ok and [v.rule for v in result.violations] == ["metric"]
    assert rule_checks.gate_problem(result, ws.branch)
    committed[MAIN] = VALID
    assert rule_checks.run(None, ws).ok
    committed[MAIN] = "bad"
    packs.clear()
    assert rule_checks.run(None, ws) is None


@pytest.mark.variant("fenced examples and comments cannot stand in for real deliverable sections")
def test_examples_do_not_satisfy_format():
    text = VALID.replace("## Results", "```md\n## Results\n```")
    text = text.replace("## Conclusion", "<!-- ## Conclusion -->")
    assert {v.rule for v in checker.check_main("analysis", MAIN, text, [APPENDIX])} >= {
        "results", "conclusion"}


def test_yaml_and_reference_links():
    text = VALID.replace("scope: user\n", "").replace("question: What changed?", "question: |\n  What changed?")
    text = text.replace("[Data](appendices/data.md)", "[Data][source]\n\n[source]: appendices/data.md")
    assert checker.check_main("analysis", MAIN, text, [APPENDIX]) == []
    text = text.replace("[Data][source]", "[source]")
    assert checker.check_main("analysis", MAIN, text, [APPENDIX]) == []
    assert checker.check_branch("unused", ["unrelated.md"], "analysis") == []
