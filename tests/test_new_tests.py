from brindle import new_tests

BASE = "import pytest\n@pytest.mark.parametrize('n', [1, 2])\ndef test_old(n): pass\n"
HEAD = (BASE.replace("[1, 2]", "[1, 2, (3, 'edge')]")
        + "def test_added_worth_committing(): pass\n")


def test_reviewer_brief_lists_new_tests_and_rows_worth_committing(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_a.py").write_text(HEAD)

    def fake_git(args, cwd):
        return "tests/test_a.py" if args[0] == "diff" else BASE

    monkeypatch.setattr(new_tests.git, "out", fake_git)
    brief = new_tests.review_section(tmp_path, "main")
    assert "- tests/test_a.py::test_added_worth_committing\n" in brief
    assert "- tests/test_a.py::test_old new row: (3, 'edge')\n" in brief
    assert "new row: 1" not in brief and "blocker" in brief
    monkeypatch.setattr(new_tests.git, "out", lambda args, cwd: "")
    assert new_tests.review_section(tmp_path, "main") == ""
