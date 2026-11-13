"""Ticket 4: click.get_app_dir follows XDG rules and offers data, cache and state."""

import os
import sys

import pytest

import click

XDG_VARS = ["XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"]
KIND_VARS = {
    "config": "XDG_CONFIG_HOME",
    "data": "XDG_DATA_HOME",
    "cache": "XDG_CACHE_HOME",
    "state": "XDG_STATE_HOME",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake home folder with no XDG variables set."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    for var in XDG_VARS:
        monkeypatch.delenv(var, raising=False)
    return str(path)


@pytest.fixture
def linux(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    return home


@pytest.fixture
def macos(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    return home


def test_config_ignores_empty_xdg_variable(linux, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert click.get_app_dir("Foo Bar") == os.path.join(linux, ".config", "foo-bar")


@pytest.mark.parametrize("value", ["config", "./config", "~/cfg", "cfg/"])
def test_config_ignores_relative_xdg_variable(linux, monkeypatch, value):
    monkeypatch.setenv("XDG_CONFIG_HOME", value)
    assert click.get_app_dir("Foo Bar") == os.path.join(linux, ".config", "foo-bar")


def test_data_dir_default(linux):
    assert click.get_app_dir("Foo Bar", kind="data") == os.path.join(
        linux, ".local", "share", "foo-bar"
    )


def test_cache_dir_default(linux):
    assert click.get_app_dir("Foo Bar", kind="cache") == os.path.join(
        linux, ".cache", "foo-bar"
    )


def test_state_dir_default(linux):
    assert click.get_app_dir("Foo Bar", kind="state") == os.path.join(
        linux, ".local", "state", "foo-bar"
    )


@pytest.mark.parametrize("kind", ["config", "data", "cache", "state"])
def test_absolute_xdg_variable_is_used(linux, monkeypatch, tmp_path, kind):
    target = str(tmp_path / f"xdg-{kind}")
    monkeypatch.setenv(KIND_VARS[kind], target)
    assert click.get_app_dir("Foo Bar", kind=kind) == os.path.join(target, "foo-bar")


@pytest.mark.parametrize(
    ("kind", "default"),
    [
        ("data", os.path.join(".local", "share")),
        ("cache", ".cache"),
        ("state", os.path.join(".local", "state")),
    ],
)
@pytest.mark.parametrize("value", ["", "some/dir"])
def test_empty_or_relative_xdg_variable_is_ignored(
    linux, monkeypatch, kind, default, value
):
    monkeypatch.setenv(KIND_VARS[kind], value)
    assert click.get_app_dir("Foo Bar", kind=kind) == os.path.join(
        linux, default, "foo-bar"
    )


def test_each_kind_reads_only_its_own_variable(linux, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert click.get_app_dir("Foo Bar", kind="cache") == os.path.join(
        linux, ".cache", "foo-bar"
    )
    assert click.get_app_dir("Foo Bar", kind="data") == os.path.join(
        str(tmp_path / "data"), "foo-bar"
    )


@pytest.mark.parametrize("kind", ["logs", "", "Config", "CACHE"])
def test_unknown_kind_is_rejected(linux, kind):
    with pytest.raises(ValueError):
        click.get_app_dir("Foo Bar", kind=kind)


def test_force_posix_config_kind_is_the_dot_folder(linux):
    assert click.get_app_dir("Foo Bar", force_posix=True, kind="config") == (
        os.path.join(linux, ".foo-bar")
    )


@pytest.mark.parametrize("kind", ["data", "cache", "state"])
def test_force_posix_other_kinds_are_inside_the_dot_folder(linux, kind):
    assert click.get_app_dir("Foo Bar", force_posix=True, kind=kind) == (
        os.path.join(linux, ".foo-bar", kind)
    )


def test_macos_cache_dir(macos):
    assert click.get_app_dir("Foo Bar", kind="cache") == os.path.join(
        macos, "Library", "Caches", "Foo Bar"
    )


@pytest.mark.parametrize("kind", ["config", "data", "state"])
def test_macos_other_kinds_use_application_support(macos, kind):
    assert click.get_app_dir("Foo Bar", kind=kind) == os.path.join(
        macos, "Library", "Application Support", "Foo Bar"
    )


def test_macos_ignores_xdg_variables(macos, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    assert click.get_app_dir("Foo Bar", kind="cache") == os.path.join(
        macos, "Library", "Caches", "Foo Bar"
    )


def test_folders_are_not_created(linux):
    for kind in ["config", "data", "cache", "state"]:
        assert not os.path.exists(click.get_app_dir("Foo Bar", kind=kind))
