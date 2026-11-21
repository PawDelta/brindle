from dataclasses import replace
from types import SimpleNamespace

import pytest

from brindle import permissions


@pytest.mark.parametrize("decision, profile", [
    pytest.param("deny", False, id="deny", marks=pytest.mark.variant("deny rules bind possible expansions (base case of the bypass)")),
    pytest.param("ask", False, id="ask", marks=pytest.mark.variant("ask rules also bind possible expansions")),
    pytest.param("deny", True, id="profile", marks=pytest.mark.variant("profile glob denies also bind possible expansions")),
])
def test_unmatched_glob_cannot_bypass_an_overlapping_restriction(tmp_path, decision, profile):
    here = str(tmp_path)
    req = permissions.Request("claude", "bash", "Bash", here, here, here, command="gh api bl?cked?")
    rules = (permissions.profile_rules(SimpleNamespace(permission_denies=["bash glob gh api blocked*"]))
             if profile else [permissions.Rule("bash", "gh api blocked*", "glob", decision)])
    # No file matches yet, but blockedX could appear before the shell runs.
    assert permissions.rule_matches(rules[0], replace(req, command="gh api blockedX"))
    assert permissions.decide(req, rules=rules, unmatched="allow", saved=[], groups={}).decision in ("ask", "deny")


def test_unmatched_query_glob_keeps_the_prompted_api_read_literal(tmp_path):
    # Permission history, 2026-11-13 15:43:42 UTC: this read prompted.
    command = (
        'gh api repos/loro-dev/loro-py/commits?per_page=5 '
        r'--jq ".[] | .commit.committer.date + \" \" + (.commit.message | split(\"\n\")[0])"'
    )
    here = str(tmp_path)
    req = permissions.Request("claude", "bash", "Bash", here, here, here, command=command)
    permissions.apply_preset("standard")
    decision = permissions.decide(req)
    assert decision.decision == "allow", decision.reason

    # A literal argument is still subject to the person's own restrictions.
    permissions.add_rule("bash", "gh api repos/loro-dev/loro-py/commits?per_page=5", "deny", "prefix")
    assert permissions.decide(req).decision == "deny"


def test_query_globs_still_require_proof_when_the_shell_can_change_arguments(tmp_path):
    here = str(tmp_path)
    req = permissions.Request("claude", "bash", "Bash", here, here, here,
                              command="gh api commits?per_page=5")
    permissions.apply_preset("standard")
    (tmp_path / "commitsXper_page=5").touch()
    assert permissions.decide(req).decision == "ask"

    # A preceding command may create a match that does not exist at check time.
    assert permissions.decide(replace(req, command="touch new; gh api missing?match")).decision == "ask"

    # zsh's recursive glob can match zero directories, unlike the bounded matcher.
    (tmp_path / "blocked").touch()
    permissions.add_rule("bash", "gh api blocked", "deny", "prefix")
    assert permissions.decide(replace(req, command="gh api blocked")).decision == "deny"
    assert permissions.decide(replace(req, command="gh api **/blocked")).decision == "ask"

    # nullglob removes an unmatched argument, bringing a denied prefix together.
    permissions.apply_preset("autonomous")
    permissions.add_rule("bash", "gh api blocked", "deny", "prefix")
    req = replace(req, command="bash -O nullglob -c 'gh api missing?match blocked'")
    assert permissions.decide(req).decision != "allow"


OWNER_COMMAND = ("W=~/Projects/pawdelta-web; git -C $W branch --show-current; "
                 "git -C $W log origin/main -1 --format='%h %ci' ; "
                 "git -C $W show origin/main:backend/pro_api/analysis_share_links.py > /dev/null && echo exists-on-main")


@pytest.mark.parametrize("command, decision, rule", [
    # Permission history, 2026-11-20: a worker's read-only command went to the
    # owner as a d-shell-syntax ask, since W's value started with ~.
    pytest.param(OWNER_COMMAND, "allow", None,
                 marks=pytest.mark.variant("the owner's reported command: ~ in a value, redirect and && echo")),
    pytest.param("W=$HOME/p; git -C $W push origin HEAD:feature", "allow", None,
                 marks=pytest.mark.variant("$HOME in a value, where a push rule is in force")),
    pytest.param('W=$HOME/p; git -C $W push origin HEAD:feature; echo "$USER" "$(pwd)"', "allow", None,
                 marks=pytest.mark.variant("an argument only the run knows can't change HOME")),
    pytest.param("W=$HOME/p; git -C $W push origin HEAD:feature; echo alias read; git log --grep=export",
                 "allow", None, marks=pytest.mark.variant("a setter's name as an argument is only data")),
    pytest.param("D=~/p; rm -rf $D/build", "allow", None,
                 marks=pytest.mark.variant("~ in a value, where the rm rule is in force")),
    # What the substituted value names is still checked like the literal path.
    pytest.param("H=~; rm -rf $H", "deny", "d-rm",
                 marks=pytest.mark.variant("substitution of ~ reveals a destructive rm of the home")),
    pytest.param("H=$HOME; rm -rf $H", "deny", "d-rm",
                 marks=pytest.mark.variant("substitution of $HOME reveals a destructive rm of the home")),
    # A quoted ~ is not the home: the value stays unknown, so a possible deny still holds it.
    pytest.param("W='~'; rm -rf $W", "deny", "d-shell-syntax",
                 marks=pytest.mark.variant("a quoted tilde is never resolved")),
])
def test_a_variable_assigned_a_home_path_is_checked_as_its_value(tmp_path, command, decision, rule):
    here = str(tmp_path)
    permissions.apply_preset("autonomous")
    got = permissions.decide(permissions.Request("claude", "bash", "Bash", here, here, here, command=command))
    assert (got.decision, got.rule.id if got.rule else None) == (decision, rule), got.reason


@pytest.mark.parametrize("command", [
    pytest.param('HOME=blocked; git commit -m "$(gh api $HOME)"', id="reassigned",
                 marks=pytest.mark.variant("a substitution re-read alone must not take $HOME for the home")),
    pytest.param('HOME=$(pwd); git commit -m "$(gh api $HOME)"', id="computed",
                 marks=pytest.mark.variant("a home only the run knows stays unknown in a nested script")),
    pytest.param('read HOME; git commit -m "$(gh api $HOME)"', id="read",
                 marks=pytest.mark.variant("a builtin that may set HOME")),
    pytest.param('n=HOME; eval "$n=blocked"; git commit -m "$(gh api $HOME)"', id="eval",
                 marks=pytest.mark.variant("an eval may set HOME under a built-up name")),
    pytest.param('HOM\\\nE=blocked; git commit -m "$(gh api $HOME)"', id="continuation",
                 marks=pytest.mark.variant("a line continuation splits the name as written")),
    pytest.param("n=HO'ME'; re'ad' \"$n\"; git commit -m \"$(gh api $HOME)\"", id="quoted",
                 marks=pytest.mark.variant("quotes split a setter's and the variable's names as written")),
    pytest.param('c=re; n=HO; ${c}ad ${n}ME; git commit -m "$(gh api $HOME)"', id="computed-name",
                 marks=pytest.mark.variant("a command name only the run knows may be a setter")),
    pytest.param('c=a; n=HO; re${c}d ${n}ME <<< blocked; git commit -m "$(gh api $HOME)"', id="mid-word",
                 marks=pytest.mark.variant("an expansion inside a command name")),
    pytest.param('c=re; n=HO; command -p ${c}ad ${n}ME; git commit -m "$(gh api $HOME)"', id="wrapper-option",
                 marks=pytest.mark.variant("a computed command name after a wrapper's option")),
    pytest.param('git commit -m "$(c=re; a=ad; n=HO; m=ME; $c$a $n$m <<< blocked; gh api $HOME)"',
                 id="inside-a-substitution",
                 marks=pytest.mark.variant("a computed setter inside a substitution given as an argument")),
    pytest.param('c=$(printf re)ad; n=$(printf HO)ME; cat <($c -r $n <<< blocked; gh api $HOME)',
                 id="process-substitution",
                 marks=pytest.mark.variant("a process substitution's first word names a command")),
    pytest.param('shopt -s expand_aliases; c=$(printf re)ad; alias r=$c\nr $(printf HO)ME <<< blocked; '
                 'git commit -m "$(gh api $HOME)"', id="alias",
                 marks=pytest.mark.variant("an alias makes a literal word a setter")),
    pytest.param('{re,}ad {HO,}ME; git commit -m "$(gh api $HOME)"', id="braces",
                 marks=pytest.mark.variant("brace expansion builds a setter and HOME")),
])
def test_home_the_command_may_change_stays_unknown_in_its_substitutions(tmp_path, command):
    here = str(tmp_path)
    rules = [permissions.Rule("bash", "gh api blocked", "prefix", "deny")]
    req = permissions.Request("claude", "bash", "Bash", here, here, here, command=command)
    assert permissions.decide(req, rules=rules, unmatched="allow", saved=[], groups={}).decision == "deny"