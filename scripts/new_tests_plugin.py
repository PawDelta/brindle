"""A pytest plugin (``-p new_tests_plugin``) for scripts/check_new_tests.py.

* With BRINDLE_SHUFFLE_SEED set, puts the tests in a random but repeatable order, so
  new and changed tests can be repeated in different orders. Every xdist worker
  applies the same seed, so they agree on the order.
* With BRINDLE_SKIPPED_FILE set, appends the node id of every skipped test to that
  file, so a test that recorded no coverage can be told from one that was skipped.
"""
import os
import random


def pytest_collection_modifyitems(session, config, items):
    seed = os.environ.get("BRINDLE_SHUFFLE_SEED")
    if seed:
        random.Random(seed).shuffle(items)


def pytest_runtest_logreport(report):
    path = os.environ.get("BRINDLE_SKIPPED_FILE")
    if path and report.skipped:
        with open(path, "a", encoding="utf-8") as out:
            out.write(report.nodeid + "\n")
