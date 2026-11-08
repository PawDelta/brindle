"""A pytest plugin (``-p shuffle_tests``) that puts the tests in a random but repeatable
order when BRINDLE_SHUFFLE_SEED is set; scripts/check_new_tests.py uses it to repeat
new and changed tests in different orders. Every xdist worker applies the same seed,
so they agree on the order."""
import os
import random


def pytest_collection_modifyitems(session, config, items):
    seed = os.environ.get("BRINDLE_SHUFFLE_SEED")
    if seed:
        random.Random(seed).shuffle(items)
