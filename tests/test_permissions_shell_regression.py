from dataclasses import replace

from brindle import permissions


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
