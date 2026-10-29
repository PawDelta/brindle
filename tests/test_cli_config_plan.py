"""`brindle config set plan` writes the person's default or the repo's override."""

import pytest
from typer.testing import CliRunner

from brindle import config, config_cmds
from brindle.cli import app

runner = CliRunner()


@pytest.mark.parametrize("args, user, local, code", [
    (["config", "set", "plan", "always"], "always", None, 0),
    (["config", "set", "plan", "never", "--project"], None, "never", 0),
    (["config", "set", "plan", "sometimes"], None, None, 1),
])
def test_config_set_plan(args, user, local, code, tmp_path, monkeypatch):
    monkeypatch.setattr(config_cmds, "_root", lambda: str(tmp_path))
    r = runner.invoke(app, args)
    assert r.exit_code == code
    assert config.user_settings().get("plan") == user
    assert config.plan_layers(tmp_path)["local"] == local
