"""brindle.crdt against a JS Loro peer: the app's editor runs loro-crdt and
counts UTF-16 units, the engine counts code points."""

import base64
import json
import shutil

import pytest

from brindle import spawn
from brindle.crdt import SharedDoc, code_point_offset, utf16_offset

LORO_JS = "loro-crdt@1.17.1"

# A JS peer writes a section with an emoji in it.
WRITE = """
import { LoroDoc } from "loro-crdt";
const d = new LoroDoc();
d.setPeerId(1);
d.getText("intro").insert(0, "a\\u{1F600}b");
d.commit();
process.stdout.write(Buffer.from(d.export({ mode: "snapshot" })).toString("base64"));
"""

# The same JS peer takes the engine's update and resolves the engine's cursor.
READ = """
import { readFileSync } from "node:fs";
import { LoroDoc, Cursor } from "loro-crdt";
const [snapshot, update, cursor] = JSON.parse(readFileSync(0, "utf8")).map((s) => Buffer.from(s, "base64"));
const d = new LoroDoc();
d.import(snapshot);
d.import(update);
const pos = d.getCursorPos(Cursor.decode(cursor));
process.stdout.write(JSON.stringify({ text: d.getText("intro").toString(), cursor: pos.offset }));
"""


@pytest.fixture(scope="module")
def node_loro(tmp_path_factory):
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        pytest.skip("node and npm are needed to run a JS Loro peer")
    where = tmp_path_factory.mktemp("loro-js")
    got = spawn.action([npm, "install", "--no-audit", "--no-fund", "--silent", LORO_JS], cwd=where)
    if got.returncode != 0:
        pytest.skip(f"could not install {LORO_JS} from npm: {got.stderr.strip()[-300:]}")

    def run(program: str, stdin: str = "") -> str:
        out = spawn.action([node, "--input-type=module", "-e", program], cwd=where, input=stdin)
        assert out.returncode == 0, out.stderr
        return out.stdout
    return run


@pytest.mark.slow("installs loro-crdt from npm and runs node twice")
def test_insert_after_emoji_lands_in_place_for_a_js_peer(node_loro):
    snapshot = base64.b64decode(node_loro(WRITE))
    doc = SharedDoc.load(snapshot)
    since = doc.version()
    before_b = doc.cursor("intro", 2)       # code points: a, emoji, b

    doc.insert("intro", 2, "X")             # right after the emoji

    assert doc.text("intro") == "a\U0001F600Xb"
    assert doc.resolve(before_b) == 3
    sent = [base64.b64encode(b).decode() for b in (snapshot, doc.export_updates(since), before_b)]
    js = json.loads(node_loro(READ, json.dumps(sent)))
    assert js["text"] == "a\U0001F600Xb"
    # The JS peer counts UTF-16 units: the emoji is two of them.
    assert js["cursor"] == utf16_offset(js["text"], 3) == 4
    assert code_point_offset(js["text"], js["cursor"]) == 3
