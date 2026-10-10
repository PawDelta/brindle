import json
import os
import time

import pytest
from typer.testing import CliRunner

from brindle.cli import app
from brindle.permissions import DEFAULT_RULES, PRESET_COMMANDS, Request, decide, load_store, store_path

runner = CliRunner()


def test_preset_permissions(monkeypatch, tmp_path):
    """Verify that preset permissions are in the defaults and allow their commands."""
    # Mock brindle_home to point to tmp_path so store_path() returns a non-existent file path
    monkeypatch.setattr("brindle.permissions.brindle_home", lambda: tmp_path)
    
    # Call load_store() which should create the file
    store = load_store()
    
    # Verify the file was written
    assert store_path().exists()
    
    rules = store.rules
    
    # Check that presets are in load_store().rules when file doesn't exist
    for cmd in PRESET_COMMANDS:
        # We can just check that a bash allow exact and prefix rule exists for each cmd
        assert any(r.kind == "bash" and r.match == cmd and r.match_type == "exact" and r.decision == "allow" for r in rules)
        assert any(r.kind == "bash" and r.match == f"{cmd} " and r.match_type == "prefix" and r.decision == "allow" for r in rules)
        
        # Test exact match
        req_exact = Request("claude", "bash", "Bash", command=cmd)
        d_exact = decide(req_exact, rules=list(rules))
        assert d_exact.decision == "allow"
        
        # Test prefix match
        req_prefix = Request("claude", "bash", "Bash", command=f"{cmd} status")
        d_prefix = decide(req_prefix, rules=list(rules))
        assert d_prefix.decision == "allow"


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
