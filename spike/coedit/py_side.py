"""T0 spike, pycrdt side. Run by run.sh after `node yjs_side.mjs 1`.

Reads what Yjs wrote (out/js_*.json), checks it, writes what pycrdt wrote
(out/py_*.json) for `node yjs_side.mjs 2` to check the other way round.
"""

import base64
import hashlib
import json
import time
import zlib
from pathlib import Path

import pycrdt
from pycrdt import Assoc, Doc, StickyIndex, Text

OUT = Path(__file__).with_name("out")
OUT.mkdir(exist_ok=True)
MARKER = "SECRET-7731"
results: list[dict] = []


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def unb64(s: str) -> bytes:
    return base64.b64decode(s)


def check(cid: str, ok, detail: str) -> None:
    results.append({"id": cid, "side": "python", "ok": ok, "detail": detail})
    tag = "PASS" if ok is True else "FAIL" if ok is False else "INFO"
    print(f"{tag} [python] {cid}: {detail}")


def runs(arr) -> str:
    out: list[list] = []
    for x in arr:
        if out and out[-1][0] == x:
            out[-1][1] += 1
        else:
            out.append([x, 1])
    return " ".join(f"{c}x{n}" for c, n in out)


def u16len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def u16_find(s: str, sub: str) -> int:
    return u16len(s[: s.index(sub)])


def u16_to_byte(s: str, i: int) -> int:
    """pycrdt's insert/delete count UTF-8 bytes; Yjs and the app count UTF-16 units."""
    return len(s.encode("utf-16-le")[: 2 * i].decode("utf-16-le").encode("utf-8"))


def read_varuint(b: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        c = b[i]
        i += 1
        n |= (c & 0x7F) << shift
        shift += 7
        if c < 0x80:
            return n, i


def state_clients(update: bytes) -> list[int]:
    """Replica numbers that have letters in an update, via pycrdt.get_state (a state vector)."""
    sv = pycrdt.get_state(update)
    n, i = read_varuint(sv, 0)
    out = []
    for _ in range(n):
        c, i = read_varuint(sv, i)
        _, i = read_varuint(sv, i)
        out.append(c)
    return sorted(out)


def state_map(doc: Doc) -> dict[int, int]:
    sv = doc.get_state()
    n, i = read_varuint(sv, 0)
    out = {}
    for _ in range(n):
        c, i = read_varuint(sv, i)
        k, i = read_varuint(sv, i)
        out[c] = k
    return out


def load(updates: list[bytes]) -> tuple[Doc, Text]:
    d = Doc(skip_gc=False)
    for u in updates:
        d.apply_update(u)
    return d, d.get("t", type=Text)


def b2u16(s: str, byte: int) -> int:
    """UTF-16 index of the character that starts at UTF-8 byte offset `byte`."""
    return u16len(s.encode("utf-8")[:byte].decode("utf-8"))


def per_letter(t: Text, by_bytes: bool = True) -> list[int]:
    """Replica number of the letter at each UTF-16 index.

    pycrdt's sticky_index only gives right answers for UTF-8 byte offsets of
    character starts (see the fuzz check), so walk the characters by bytes and
    repeat a character's number for each UTF-16 unit it takes.
    """
    out: list[int] = []
    byte = idx16 = 0
    for ch in str(t):
        n16 = u16len(ch)
        if by_bytes:
            c = t.sticky_index(byte, Assoc.AFTER).to_json()["item"]["client"]
        else:
            c = t.sticky_index(idx16, Assoc.AFTER).to_json()["item"]["client"]
        out += [c] * n16
        byte += len(ch.encode())
        idx16 += n16
    return out


def resolve(t: Text, pos_b64: str):
    """UTF-16 index a travelling position resolves to (pycrdt's get_index counts bytes)."""
    try:
        return b2u16(str(t), StickyIndex.decode(unb64(pos_b64), t).get_index())
    except BaseException as e:  # pycrdt's Rust panics are BaseException
        return f"{type(e).__name__}"


def compact(updates: list[bytes]) -> tuple[Doc, Text, bytes]:
    d, t = load(updates)
    return d, t, d.get_update()


def has(b: bytes, s: str) -> bool:
    return s.encode() in b


def verify(tag: str, d: dict) -> None:
    log = [unb64(x) for x in d["log"]]
    doc, t = load(log)
    text = str(t)
    check(f"{tag}:read-text", text == d["finalText"], f"text read back by pycrdt {'matches' if text == d['finalText'] else 'DIFFERS'} ({u16len(text)} UTF-16 units)")
    live = resolve(t, d["posLive"])
    check(f"{tag}:position-live", live == d["expectLive"], f"travelling position resolves to {live}, expected {d['expectLive']} (UTF-16 units)")
    cdoc, ct, cupd = compact(log)
    raw_has = any(has(u, d["marker"]) for u in log)
    c_has = has(cupd, d["marker"])
    check(f"{tag}:deleted-text-gone", raw_has and not c_has, f"deleted text in raw log: {raw_has}; in compacted state: {c_has} (raw {sum(map(len, log))} B, compacted {len(cupd)} B)")
    check(f"{tag}:compacted-text", str(ct) == d["finalText"], "compacted state reads back the same text")
    l2 = resolve(ct, d["posLive"])
    l3 = resolve(ct, d["posDead"])
    check(f"{tag}:old-position-after-compaction", l2 == d["expectLive"], f"position made before the delete resolves to {l2} after compaction, expected {d['expectLive']}")
    check(f"{tag}:position-inside-deleted-text", l3 == d["expectDead"], f"position inside the later-deleted words resolves to {l3} after compaction, expected {d['expectDead']}")
    before = per_letter(t)
    after = per_letter(ct)
    check(f"{tag}:replica-per-letter-before", before == d["clientsBefore"], f"pycrdt reads the same replica number per letter as the author did: {runs(before)}")
    check(f"{tag}:replica-per-letter-after-compaction", before == after, f"unchanged by compaction: {runs(after)}")
    bc = state_clients(unb64(d["batch"]))
    check(f"{tag}:replica-numbers-in-batch", bc == d["batchClients"], f"pycrdt lists the numbers inside one batch as {bc}, author said {d['batchClients']}")


def author(c1: int, c2: int) -> dict:
    A = Doc(client_id=c1)
    B = Doc(client_id=c2)
    log: list[bytes] = []
    quiet = [False]

    def tap(ev):
        if not quiet[0]:
            log.append(ev.update)

    A.observe(tap)
    B.observe(tap)
    tA = A.get("t", type=Text)
    tB = B.get("t", type=Text)
    tA.insert(0, f"Hello 😀 world, {MARKER} here. 🎉 end")
    quiet[0] = True
    B.apply_update(log[0])
    quiet[0] = False
    s = str(tB)
    tB.insert(u16_to_byte(s, u16_find(s, "world")), "dear ")
    tB.insert(len(str(tB).encode()), " — café 𝄞")
    quiet[0] = True
    A.apply_update(log[1])
    A.apply_update(log[2])
    quiet[0] = False
    old = str(tA)
    pos_live = tA.sticky_index(old.encode().index(b"here"), Assoc.AFTER).encode()
    pos_dead = tA.sticky_index(old.encode().index(MARKER.encode()) + 3, Assoc.AFTER).encode()
    batch = pycrdt.merge_updates(log[0], log[1])
    start = u16_to_byte(old, u16_find(old, MARKER))
    del tA[start : start + len((MARKER + " ").encode())]
    final = str(tA)
    return {
        "log": [b64(u) for u in log],
        "finalText": final,
        "marker": MARKER,
        "expectLive": u16_find(final, "here"),
        "expectDead": u16_find(final, "here"),
        "posLive": b64(pos_live),
        "posDead": b64(pos_dead),
        "clientsBefore": per_letter(tA),
        "batch": b64(batch),
        "batchClients": state_clients(batch),
    }


def units() -> None:
    d = Doc(client_id=7)
    t = d.get("t", type=Text)
    t += "a😀b"
    s = str(t)
    ok16 = len(t) == u16len(s)
    check(
        "units:pycrdt-text-length",
        ok16,
        f'"a😀b": Yjs counts {u16len(s)} UTF-16 units, pycrdt len() says {len(t)} (UTF-8 bytes: {len(s.encode())}, code points: {len(s)})',
    )
    t.insert(3, "X")
    check("units:pycrdt-insert-offset", str(t) == "a😀Xb", f'insert(3, "X") into "a😀b" gives {str(t)!r}; a UTF-16 index 3 is after the emoji, so pycrdt\'s insert is NOT in UTF-16 units (it took bytes)' if str(t) != "a😀Xb" else "insert offset is UTF-16")
    fuzz()


def fuzz() -> None:
    """sticky_index / get_index on mixed-width text, judged against what Yjs says."""
    docs = json.loads((OUT / "js_fuzz.json").read_text())
    n_letters = bad16 = badbytes = 0
    n_pos = bad_pos_bytes = bad_pos_u16 = bad_pos_id = 0
    panics = 0
    for d in docs:
        doc = Doc()
        doc.apply_update(unb64(d["update"]))
        t = doc.get("t", type=Text)
        assert str(t) == d["text"]
        for mode in (True, False):
            try:
                got = per_letter(t, by_bytes=mode)
            except BaseException:
                got = None
                panics += 1
            wrong = got != d["clients"]
            if mode:
                badbytes += wrong
            else:
                bad16 += wrong
        n_letters += len(d["clients"])
        s = str(t)
        # workaround: find each letter's id once, then look a position's id up
        by_id: dict = {}
        byte = idx16 = 0
        for ch in s:
            it = t.sticky_index(byte, Assoc.AFTER).to_json()["item"]
            by_id[(it["client"], it["clock"])] = idx16
            byte += len(ch.encode())
            idx16 += u16len(ch)
        for p in d["positions"]:
            n_pos += 1
            try:
                it = StickyIndex.decode(unb64(p["pos"]), t).to_json()["item"]
                if by_id.get((it["client"], it["clock"])) != p["idx"]:
                    bad_pos_id += 1
            except BaseException:
                bad_pos_id += 1
            try:
                raw = StickyIndex.decode(unb64(p["pos"]), t).get_index()
            except BaseException:
                raw = None
            try:
                conv = None if raw is None else b2u16(s, raw)
            except UnicodeDecodeError:  # points into the middle of a character
                conv = None
            if conv != p["idx"]:
                bad_pos_bytes += 1
            if raw != p["idx"]:
                bad_pos_u16 += 1
    check(
        "units:sticky_index-by-utf8-bytes",
        badbytes == 0,
        f"{len(docs)} random 3-writer docs with é 中 😀 𝄞 (about {n_letters} letters): per-letter replica read with sticky_index(UTF-8 byte offset) wrong in {badbytes} docs; with sticky_index(UTF-16 index) wrong in {bad16} docs ({panics} panics)",
    )
    check(
        "units:position-resolution-fuzz",
        bad_pos_bytes == 0,
        f"{n_pos} Yjs travelling positions on those docs: StickyIndex.decode().get_index() converted from bytes to UTF-16 is wrong for {bad_pos_bytes}; taking its result as UTF-16 directly is wrong for {bad_pos_u16} (it mixes bytes and UTF-16 units inside one text)",
    )
    check(
        "units:position-resolution-by-id-workaround",
        bad_pos_id == 0,
        f"same {n_pos} positions resolved by looking the position's (replica, clock) up among the letters' ids instead of calling get_index: wrong for {bad_pos_id} (works only while the letter still exists; not for words that were deleted)",
    )


def replicas() -> None:
    js = json.loads((OUT / "js_r.json").read_text())
    for e in js["written"]:
        n = e["n"]
        doc, t = load([unb64(e["update"])])
        got = per_letter(t)
        check(f"replica-set:yjs-read-in-pycrdt:{n}", all(x == n for x in got) and str(t) == "x😀y", f"pycrdt reads Yjs's letters as {sorted(set(got))}, text {str(t)!r}")
    written = []
    for n in [1, 7, 2**31, 2**32 - 1, 2**53 - 1]:
        try:
            d = Doc(client_id=n)
            t = d.get("t", type=Text)
            t += "x😀y"
            got = per_letter(t)
            ok = all(x == n for x in got) and d.client_id == n
            check(f"replica-set:pycrdt:{n}", ok, f"pycrdt writes under {n}: letters carry {sorted(set(got))}, doc.client_id={d.client_id}")
            written.append({"n": n, "update": b64(d.get_update())})
        except BaseException as e:
            check(f"replica-set:pycrdt:{n}", False, f"{type(e).__name__}: {e}")
            written.append({"n": n, "error": f"{type(e).__name__}: {e}"})
    (OUT / "py_r.json").write_text(json.dumps({"written": written}))

    def reload_and_write(n: int, store: bytes) -> tuple[int, int, bytes]:
        d = Doc(client_id=n)
        d.apply_update(store)
        before = state_map(d)[n]
        t = d.get("t", type=Text)
        t += " more🎉"
        peer = Doc()
        peer.apply_update(store)
        return before, state_map(d)[n], d.get_update(peer.get_state())

    def peer_ok(n: int, store: bytes, delta: bytes) -> bool:
        peer = Doc()
        peer.apply_update(store)
        peer.apply_update(delta)
        t = peer.get("t", type=Text)
        return str(t) == "x😀y more🎉" and all(x == n for x in per_letter(t))

    # stored in pycrdt, reloaded in pycrdt
    n = 4343
    d = Doc(client_id=n)
    d.get("t", type=Text).__iadd__("x😀y")
    store = d.get_update()
    before, after, delta = reload_and_write(n, store)
    check("replica-reload:pycrdt-self", after > before and before == 4, f"reloaded replica {n} clock {before} -> {after}")
    check("replica-reload:pycrdt-peer-converges", peer_ok(n, store, delta), "peer holding the stored copy applies the delta: text and replica numbers right")
    (OUT / "py_reload.json").write_text(json.dumps({"n": n, "store": b64(store), "delta": b64(delta)}))
    # stored in Yjs, reloaded in pycrdt under the same number
    jr = json.loads((OUT / "js_reload.json").read_text())
    jstore = unb64(jr["store"])
    before, after, delta = reload_and_write(jr["n"], jstore)
    check("replica-reload:pycrdt-reloads-yjs-store", after > before, f"pycrdt reloads the Yjs store under {jr['n']}: clock {before} -> {after}")
    check("replica-reload:pycrdt-reloads-yjs-store-peer", peer_ok(jr["n"], jstore, delta), "a peer holding the Yjs-stored copy applies pycrdt's delta: text and replica numbers right")
    # negative: same number, no stored state
    lost = Doc(client_id=n)
    lost.get("t", type=Text).__iadd__("Q")
    peer = Doc()
    peer.apply_update(store)
    peer.apply_update(lost.get_update())
    pt = str(peer.get("t", type=Text))
    check("replica-reload:pycrdt-without-state", None, f"same number but no stored state: peer text is {pt!r} (the new letter {'was kept' if 'Q' in pt else 'was silently dropped'})")


def weigh() -> None:
    rows = json.loads((OUT / "js_weigh.json").read_text())
    for r in rows:
        data = (OUT / f"weigh_{r['kind']}.bin").read_bytes()
        t0 = time.monotonic()
        d = Doc(skip_gc=False)
        d.apply_update(data)
        t = d.get("t", type=Text)
        text = str(t)
        t1 = time.monotonic()
        re = d.get_update()
        t2 = time.monotonic()
        same = hashlib.sha256(text.encode()).hexdigest() == r["sha"]
        limit = 350 * 1024
        verdict = "fits" if len(re) <= limit and r["deflated"] <= limit else "does NOT fit"
        check(
            f"weigh:{r['kind']}",
            len(re) <= limit and same,
            f"text {r['textBytes']} B ({r['textBytes'] / 1024:.0f} KiB, {r['ops']} edits); snapshot v1 {r['compacted']} B ({r['compacted'] / 1024:.0f} KiB), deflated {r['deflated']} B ({r['deflated'] / 1024:.0f} KiB), v2 {r['v2']} B ({r['v2'] / 1024:.0f} KiB; pycrdt has no v2 reader), v2 deflated {r['v2deflated']} B; "
            f"pycrdt loads it in {1000 * (t1 - t0):.0f} ms, text identical: {same}, re-encodes to {len(re)} B ({len(re) / 1024:.0f} KiB) in {1000 * (t2 - t1):.0f} ms, deflated {len(zlib.compress(re))} B; 350 KiB limit {verdict}",
        )


def main() -> None:
    print("pycrdt", pycrdt.__version__)
    units()
    jd = json.loads((OUT / "js_a.json").read_text())
    verify("pycrdt-reads-yjs", jd)
    pd = author(201, 202)
    (OUT / "py_a.json").write_text(json.dumps(pd))
    print("final text:", json.dumps(pd["finalText"], ensure_ascii=False), "letters:", runs(pd["clientsBefore"]))
    verify("pycrdt-reads-pycrdt", pd)
    replicas()
    weigh()
    (OUT / "results_py.json").write_text(json.dumps(results))


main()
