import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "kb_eval", Path(__file__).resolve().parent.parent / "scripts/kb_eval.py")
kb_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kb_eval)


def test_eval_scores_fixture(tmp_path):
    corpus = tmp_path / "tests" / "kb_eval" / "corpus"
    corpus.mkdir(parents=True)
    (corpus / "pets.md").write_text(
        "# Pets\n\n## Cats\n\nCats purr when content.\n\n```sh\n# not a heading\n```\n\n"
        "## Dogs\n\nDogs fetch sticks.\n")
    (corpus / "tools.md").write_text("# Tools\n\n## Hammer\n\nA hammer drives nails.\n")
    (corpus / "secret.md").write_text("---\nvisibility: private\n---\n# Vault\n\nThe vault code is swordfish.\n")
    questions = [
        {"id": "a", "tag": "single-fact", "question": "What do cats do when content?",
         "expected": [{"file": "tests/kb_eval/corpus/pets.md", "section": "Cats"}]},
        {"id": "b", "tag": "single-fact", "question": "Who plays the bagpipes?",
         "expected": [{"file": "tests/kb_eval/corpus/pets.md", "section": "Dogs"}]},
        {"id": "c", "tag": "multi-hop", "question": "Which animal does fetch and what drives nails, a hammer?",
         "expected": [{"file": "tests/kb_eval/corpus/pets.md", "section": "Dogs"},
                      {"file": "tests/kb_eval/corpus/tools.md", "section": "Hammer"},
                      {"file": "tests/kb_eval/corpus/pets.md", "section": "Fish"}]},
        {"id": "d", "tag": "private", "question": "What is the vault code, swordfish?",
         "private_file": "tests/kb_eval/corpus/secret.md", "expected": []},
    ]
    qfile = tmp_path / "questions.json"
    qfile.write_text(json.dumps(questions))
    report = tmp_path / "report.md"

    assert kb_eval.main(["--root", str(tmp_path), "--questions", str(qfile), "--report", str(report)]) == 0

    text = report.read_text()
    assert "| single-fact | 2 | 0.50 |" in text
    assert "| multi-hop | 1 | 0.67 |" in text
    assert "Private-file leaks: 0 of 1" in text
    assert "Questions missing an expected source: b, c" in text
