import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "release_footer", Path(__file__).resolve().parent.parent / "scripts" / "release_footer.py")
release_footer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_footer)


def test_footer_links_the_site_once(tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("frith 1.2.3 fixes things.\n")
    assert release_footer.footer_file(notes)
    text = notes.read_text()
    assert text.startswith("frith 1.2.3 fixes things.") and "pawdelta.com/frith" in text
    assert not release_footer.footer_file(notes)
    assert notes.read_text() == text


def test_notes_already_linking_the_site_are_left_alone():
    body = "See https://pawdelta.com/frith/ for docs."
    assert release_footer.with_footer(body) == body
