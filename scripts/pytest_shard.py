"""Opt-in, stable file sharding: python -m pytest -p scripts.pytest_shard --test-shard=1/2."""

import hashlib

import pytest


def _shard(value):
    try:
        index, count = map(int, value.split("/"))
        if 1 <= index <= count:
            return index, count
    except ValueError:
        pass
    raise pytest.UsageError("--test-shard must be INDEX/COUNT with 1 <= INDEX <= COUNT")


def pytest_addoption(parser):
    parser.addoption("--test-shard", type=_shard, help="Run one INDEX/COUNT shard by test file")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    shard = config.getoption("test_shard")
    if shard is None:
        return
    index, count = shard
    selected, deselected = [], []
    for item in items:
        path = item.path.relative_to(config.rootpath).as_posix()
        owner = int.from_bytes(hashlib.sha256(path.encode()).digest(), "big") % count + 1
        (selected if owner == index else deselected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)
