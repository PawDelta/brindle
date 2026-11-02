
import pytest
from typer.testing import CliRunner

from brindle.cli import app

runner = CliRunner()


def test_single_arg_allow():
    """Verify `brindle permissions allow <command>` with a single argument defaults to bash."""
    # We will test the CLI directly
    # Since we can't test actual db writes easily without side-effects, we can test that it doesn't crash 
    # and instead it tries to do something.
    result = runner.invoke(app, ["permissions", "allow", "aws *"])
    # It should not complain about missing 'match'
    assert "Missing argument 'match'" not in result.stdout
    # We expect it to try to add a rule, and since we run it, it might succeed and print something like
    # rule-id: allow bash exact 'aws *'
    # Actually wait, `aws *` exact match for bash rule. Let's verify output.
    assert "allow bash exact 'aws *'" in result.stdout


def test_an_agent_cannot_change_the_permission_rules(monkeypatch):
    from brindle import permissions

    monkeypatch.setenv("BRINDLE_AGENT_ID", "w1")
    before = permissions.load_store().rules
    result = runner.invoke(app, ["permissions", "allow", "curl"])
    assert result.exit_code == 1 and permissions.load_store().rules == before


@pytest.mark.parametrize("command, expected", [
    ('echo "$(git status)"', "allow"),
    ('echo "`git status`"', "allow"),
    ('git commit -m "`git diff --stat`"', "allow"),
    ("git status | grep M | cut -c1-80 | head -n 5 | tail -n 2 | sort | uniq | wc -l | tr -d ' '", "allow"),
    ("git status | awk -F: -v limit=2 -- 'NR <= limit {print $1}'", "allow"),
    ("git status | sed -e 's/old/new/g' -e '2d'", "allow"),
    ('for x in one two; do echo "$x"; git status; done', "allow"),
    ("git status; git diff && git log || git show", "allow"),
    ("cd . && git status", "allow"),
    pytest.param('echo "$(git push --force origin feature)"', "deny", marks=pytest.mark.variant("bypass attempt")),
    pytest.param('echo "`git push --force origin feature`"', "deny", marks=pytest.mark.variant("bypass attempt")),
    pytest.param('for x in status "push --force"; do git $x; done', "deny", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("cat .brindle/daemon.json | head -n 1", "deny", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status | blocked", "deny", marks=pytest.mark.variant("bypass attempt")),
    pytest.param('echo "$(blocked)"', "deny", marks=pytest.mark.variant("bypass attempt")),
    pytest.param('for x in safe blocked; do $x; done', "deny", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status; blocked", "deny", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status && blocked", "deny", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status || blocked", "deny", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("cd . && blocked", "deny", marks=pytest.mark.variant("bypass attempt")),
    pytest.param('cd "$HOME/.ssh" && awk \'{print}\' id_rsa', "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("awk *", "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param('echo "`git status; blocked`"', "deny", marks=pytest.mark.variant("bypass attempt")),
    pytest.param('echo "$(unapproved)"', "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param('echo "`unapproved`"', "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param('`echo blocked`', "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param('git push "$(echo --force)"', "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param('test "`printf secret`" = public', "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param('echo `echo \\`whoami\\``', "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status | awk '{system(\"id\")}'", "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status | awk '{\"id\" | getline x}'", "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status | awk '{print > \"out\"}'", "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status | awk 'BEGIN {ARGV[1]=\"secret\"; ARGC=2}'", "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status | awk -f program.awk", "ask", marks=pytest.mark.variant("bypass attempt")),
    ("git status | awk -F", "ask"),
    ("git status | awk -F:", "ask"),
    pytest.param("git status | awk 'BEGIN {sys\\\ntem(\"id\")}'", "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status | awk '@include \"program.awk\"'", "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status | awk '{getline x < \"secret\"}'", "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status | sed -i 's/a/b/' file", "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status | sed 'w out'", "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status | sed 'e id'", "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status | sed 's/a/id/e'", "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status | sed 's/a/b/w out'", "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status | sort -o out", "ask", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("git status | uniq input out", "ask", marks=pytest.mark.variant("bypass attempt")),
    ("python - <<'EOF'\nprint(1)\nEOF", "ask"),
    ('eval "$COMMAND"', "ask"),
    ("cat <(git status)", "ask"),
    ("case x in x) git status;; esac", "ask"),
])
def test_shell_forms(tmp_path, command, expected):
    from brindle import permissions

    permissions.apply_preset("standard")
    permissions.add_rule("bash", "git commit", "allow", "prefix")
    permissions.add_rule("bash", "blocked", "deny", "prefix")
    permissions.add_rule("bash", "test secret", "deny", "prefix")
    (tmp_path / 'BEGIN{system("id")}').write_text("")
    here = str(tmp_path)
    req = permissions.Request("claude", "bash", "Bash", here, here, here, command=command)
    decision = permissions.decide(req)
    assert decision.decision == expected, decision.reason
