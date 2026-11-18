import importlib.util
from collections import Counter
from pathlib import Path
from types import SimpleNamespace


spec = importlib.util.spec_from_file_location(
    "pytest_shard", Path(__file__).resolve().parent.parent / "scripts/pytest_shard.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def test_shards_partition_files_independently_of_checkout_and_collection_order(tmp_path):
    files = [f"tests/test_example_{i}.py" for i in range(40)]
    assignments = []
    for root in (tmp_path / "first", tmp_path / "second"):
        shards = []
        for index in (1, 2):
            items = [SimpleNamespace(path=root / file, nodeid=f"{file}::test_case[{case}]")
                     for file in files for case in range(2)]
            if root.name == "second":
                items.reverse()
            original = list(items)
            deselected = []
            config = SimpleNamespace(
                rootpath=root, getoption=lambda name: (index, 2),
                hook=SimpleNamespace(pytest_deselected=lambda items: deselected.extend(items)),
            )
            plugin.pytest_collection_modifyitems(config, items)
            assert Counter(item.nodeid for item in items + deselected) == Counter(
                item.nodeid for item in original)
            counts = Counter(item.path.relative_to(root).as_posix() for item in items)
            assert counts and all(count == 2 for count in counts.values())
            shards.append(set(counts))
        assert not shards[0] & shards[1]
        assert shards[0] | shards[1] == set(files)
        assignments.append(shards)
    assert assignments[0] == assignments[1]
