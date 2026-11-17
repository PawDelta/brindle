from brindle import new_tests

BASE = "import pytest\nCASES = [1, 2]\n@pytest.mark.parametrize('n', CASES)\ndef test_old(n): pass\n"
HEAD = ("import pytest\nCASES = [1, 2, (3, 'edge'), 1]\n"
        "@pytest.mark.parametrize('n', CASES)\ndef test_old(n): pass\n"
        "@pytest.mark.parametrize('m', [7, 8])\ndef test_added_worth_committing(m): pass\n"
        "@pytest.mark.parametrize('k', make())\ndef test_computed(k): pass\n")


def test_reviewer_brief_lists_every_new_test_and_row_worth_committing(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_a.py").write_text(HEAD)
    monkeypatch.setattr(new_tests.git, "out",
                        lambda args, cwd: "tests/test_a.py" if args[0] == "diff" else BASE)
    brief = new_tests.review_section(tmp_path, "main")
    for line in ["tests/test_a.py::test_added_worth_committing\n",
                 "test_added_worth_committing new row: 7\n", "test_added_worth_committing new row: 8\n",
                 "test_old new row: (3, 'edge')\n",
                 "test_computed parametrize table `make()` is not a literal"]:
        assert f"- tests/test_a.py::{line.split('::')[-1]}" in brief or line in brief
    assert brief.count("test_old new row: 1\n") == 1      # the duplicate of an old row is a new row
    assert "test_old new row: 2" not in brief and "blocker" in brief
    # an unchanged decorator over a computed table that gained a row still needs the reviewer's eye
    computed = "import pytest\nCASES = list(range(%d))\n@pytest.mark.parametrize('n', CASES)\ndef test_c(n): pass\n"
    (tmp_path / "tests/test_a.py").write_text(computed % 3)
    monkeypatch.setattr(new_tests.git, "out",
                        lambda args, cwd: "tests/test_a.py" if args[0] == "diff" else computed % 2)
    assert "CASES = list(range(3))` is not a literal" in new_tests.review_section(tmp_path, "main")
    monkeypatch.setattr(new_tests.git, "out", lambda args, cwd: "")
    assert new_tests.added_tests(tmp_path, "main") == []
