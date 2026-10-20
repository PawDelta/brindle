"""`brindle app` prints the download URL when the desktop app isn't installed."""

import shutil
import sys

from typer.testing import CliRunner

from brindle import cli
from brindle.cli import app


def test_app_command_prints_download_url_when_app_missing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    res = CliRunner().invoke(app, ["app"])
    assert res.exit_code == 0
    assert cli.APP_DOWNLOAD_URL in res.output
