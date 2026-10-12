
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
