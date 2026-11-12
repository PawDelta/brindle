import pytest

from brindle import permission_groups, permissions


@pytest.mark.parametrize("command, expected", [
    pytest.param("git stash apply -q abc123 && git stash drop -q "
                 '"$(git stash list --format=\'%gd %gs\' | grep tag | cut -d\' \' -f1)"', "allow",
                 marks=pytest.mark.variant("real incident 2026-11-12: the reported prompt, a read pipeline in $(...)")),
    pytest.param("S=$(git stash list --format='%H %gs' | grep tag | cut -d' ' -f1); git stash apply -q $S", "allow",
                 marks=pytest.mark.variant("reported assignment reuses substitution output in a later command")),
    pytest.param('git stash drop -q "$(git stash list --format=\'%gd %gs\' | tee out | cut -d\' \' -f1)"',
                 "ask", marks=pytest.mark.variant("bypass: a write hidden inside a substitution pipeline")),
    pytest.param('git stash drop -q "$(git stash list --format=\'%gd %gs\' | grep tag', "ask",
                 marks=pytest.mark.variant("an unclosed substitution must remain unreadable")),
    pytest.param('git stash $ACTION "$(git stash list --format=\'%gd %gs\' | cut -d\' \' -f1)"', "ask",
                 marks=pytest.mark.variant("bypass: a saved literal variable spelling cannot authorize its expansion")),
    pytest.param("rm ~+/victim", "ask",
                 marks=pytest.mark.variant("bypass: an unresolved tilde word cannot supply an allowed prefix")),
])
def test_stash_substitution_parts(tmp_path, command, expected):
    here = str(tmp_path)
    req = permissions.Request("claude", "bash", "Bash", here, here, here, command=command)
    rules = [permissions.Rule("bash", prefix, "prefix", "allow", "user") for prefix in (
        "git stash list", "git stash apply", "git stash drop", "git stash $ACTION", "grep", "cut", "rm ~+/victim",
    )] + [
        permissions.Rule("bash", "blocked", "prefix", "deny", "user"),
        permissions.Rule("bash", "tee", "prefix", "ask", "user"),
    ]
    decision = permissions.decide(req, rules=rules, unmatched="ask", saved=[])
    assert decision.decision == expected, decision.reason
    if expected == "allow":
        denied = permissions.Rule("bash", "cut", "prefix", "deny", "user")
        assert permissions.decide(req, rules=[*rules, denied], unmatched="ask", saved=[]).decision == "deny"
    elif "tee out" in command:
        assert decision.rule == rules[-1]


@pytest.mark.variant("bypass from review: a substituted filename reaching a user-denied file under a permissive baseline")
@pytest.mark.parametrize("unchecked", ["inherit", "allow"])
def test_permissive_baseline_keeps_a_deny_on_substituted_output(tmp_path, unchecked):
    # Written out, the path is denied; supplied by $(...) under a baseline that
    # runs what no rule covers (even with shell/unchecked on), it must not run.
    here = str(tmp_path)
    rules = [permissions.Rule("bash", p, "prefix", "allow", "user") for p in ("git diff", "cat")]
    rules.append(permissions.Rule("bash", "*restricted.txt*", "glob", "deny", "user"))
    groups = permission_groups.table("allow", {"shell/unchecked": "allow"}) if unchecked == "allow" else {}
    req = permissions.Request("claude", "bash", "Bash", here, here, here,
                              command='git diff --no-index /dev/null "$(cat notes.txt)"')
    assert permissions.decide(req, rules=rules, unmatched="allow", saved=[], groups=groups).decision != "allow"


@pytest.mark.parametrize("command, expected, match, match_type", [
    pytest.param("git stash $(printf clear)", "ask", "git stash clear", "prefix",
                 marks=pytest.mark.variant("bypass: substitution output supplying an excluded subcommand")),
    ("git stash drop $(printf name)", "allow", "git stash clear", "prefix"),
    pytest.param("printf name; git stash clear 'name with spaces'", "ask", "git stash clear", "prefix",
                 marks=pytest.mark.variant("bypass: exclusions must see the quoted later part, not its enclosing script")),
    pytest.param("git stash $(printf cl%s ear)", "ask", "*clear*", "glob",
                 marks=pytest.mark.variant("bypass: an exclusion with no fixed prefix can still match unknown output")),
])
def test_substitution_respects_allow_exclusions(tmp_path, command, expected, match, match_type):
    here = str(tmp_path)
    excluded = permissions.Rule("bash", match, match_type, "deny")
    rules = [permissions.Rule("bash", "git stash", "prefix", "allow", exclusions=(excluded,)),
             permissions.Rule("bash", "printf", "prefix", "allow")]
    req = permissions.Request("claude", "bash", "Bash", here, here, here, command=command)
    decision = permissions.decide(req, rules=rules, unmatched="ask", saved=[])
    assert decision.decision == expected, decision.reason
