"""Fixtures for the hidden acceptance tests (copied into the checkout by grade.sh)."""

import pytest

from click.testing import CliRunner


@pytest.fixture
def runner():
    return CliRunner()
