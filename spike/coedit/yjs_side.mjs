// T0 spike, Yjs side. Run by run.sh: `node yjs_side.mjs 1` then (after the
// Python side) `node yjs_side.mjs 2`. Files are exchanged through ./out/.
import * as Y from 'yjs'
import fs from 'node:fs'
import zlib from 'node:zlib'
import crypto from 'node:crypto'

const OUT = new URL('./out/', import.meta.url).pathname
fs.mkdirSync(OUT, { recursive: true })
const phase = process.argv[2]
const results = []
const b64 = (u) => Buffer.from(u).toString('base64')
const unb64 = (s) => new Uint8Array(Buffer.from(s, 'base64'))
const readJson = (n) => JSON.parse(fs.readFileSync(OUT + n, 'utf8'))
const writeJson = (n, o) => fs.writeFileSync(OUT + n, JSON.stringify(o))
function check(id, ok, detail) {
  results.push({ id, side: 'node', ok, detail })
  console.log(`${ok === true ? 'PASS' : ok === false ? 'FAIL' : 'INFO'} [node] ${id}: ${detail}`)
}
const has = (bytes, s) => Buffer.from(bytes).includes(Buffer.from(s, 'utf8'))
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)
const runs = (arr) => {
  const out = []
  for (const x of arr) {
    if (out.length && out[out.length - 1][0] === x) out[out.length - 1][1]++
    else out.push([x, 1])
  }
  return out.map(([c, n]) => `${c}x${n}`).join(' ')
}

const MARKER = 'SECRET-7731'

// replica number of the letter at each UTF-16 index, through the public
// relative-position API (the same idea as pycrdt's sticky_index)
function perLetter(t) {
  const out = []
  for (let i = 0; i < t.length; i++) out.push(Y.createRelativePositionFromTypeIndex(t, i).item.client)
  return out
}
function updateClients(u) {
  const dec = Y.decodeUpdate(u)
  return [...new Set(dec.structs.map((s) => s.id.client))].sort((a, b) => a - b)
}
function compact(updates) {
  const d = new Y.Doc({ gc: true })
  for (const u of updates) Y.applyUpdate(d, u)
  return { doc: d, update: Y.encodeStateAsUpdate(d) }
}

function author(c1, c2) {
  const A = new Y.Doc()
  A.clientID = c1
  const B = new Y.Doc()
  B.clientID = c2
  const log = []
  for (const d of [A, B]) d.on('update', (u, o) => { if (o !== 'sync') log.push(u) })
  const tA = A.getText('t')
  const tB = B.getText('t')
  tA.insert(0, `Hello 😀 world, ${MARKER} here. 🎉 end`)
  Y.applyUpdate(B, log[0], 'sync')
  tB.insert(tB.toString().indexOf('world'), 'dear ')
  tB.insert(tB.length, ' — café 𝄞')
  Y.applyUpdate(A, log[1], 'sync')
  Y.applyUpdate(A, log[2], 'sync')
  // a later A edit, so a batch of B's second edit + A's follow-up has clocks > 0 for both
  tA.insert(tA.length, ' !')
  const old = tA.toString()
  const posLive = Y.encodeRelativePosition(Y.createRelativePositionFromTypeIndex(tA, old.indexOf('here')))
  const posDead = Y.encodeRelativePosition(Y.createRelativePositionFromTypeIndex(tA, old.indexOf(MARKER) + 3))
  const batch = Y.mergeUpdates([log[0], log[1]])
  const batchLate = Y.mergeUpdates([log[2], log[3]])
  tA.delete(old.indexOf(MARKER), MARKER.length + 1)
  const final = tA.toString()
  return {
    log: log.slice(0, 3).concat(log.slice(3)).map(b64),
    finalText: final,
    marker: MARKER,
    expectLive: final.indexOf('here'),
    expectDead: final.indexOf('here'),
    posLive: b64(posLive),
    posDead: b64(posDead),
    clientsBefore: perLetter(tA),
    batch: b64(batch),
    // the numbers each batch should carry are known from who typed it, not read back
    batchClients: [c1, c2].sort((a, b) => a - b),
    batchLate: b64(batchLate),
    batchLateClients: [c1, c2].sort((a, b) => a - b),
  }
}

function resolve(t, doc, posB64) {
  const abs = Y.createAbsolutePositionFromRelativePosition(Y.decodeRelativePosition(unb64(posB64)), doc)
  return abs ? abs.index : null
}

function verify(tag, d, foreign) {
  const log = d.log.map(unb64)
  const doc = new Y.Doc()
  for (const u of log) Y.applyUpdate(doc, u)
  const t = doc.getText('t')
  check(`${tag}:read-text`, t.toString() === d.finalText, `text read back by Yjs ${t.toString() === d.finalText ? 'matches' : 'DIFFERS'} (${d.finalText.length} UTF-16 units)`)
  const live = resolve(t, doc, d.posLive)
  check(`${tag}:position-live`, live === d.expectLive, `travelling position resolves to ${live}, expected ${d.expectLive} (UTF-16 units)`)
  const c = compact(log)
  const rawHas = log.some((u) => has(u, d.marker))
  check(`${tag}:deleted-text-gone`, rawHas && !has(c.update, d.marker), `deleted text in raw log: ${rawHas}; in Yjs-compacted bytes: ${has(c.update, d.marker)} (raw ${log.reduce((n, u) => n + u.length, 0)} B, compacted ${c.update.length} B)`)
  const before = perLetter(t)
  check(`${tag}:replica-per-letter-before`, same(before, d.clientsBefore), `Yjs reads the same replica number per letter as the author did: ${runs(before)}`)
  // every check below runs on a FRESH doc loaded only from the encoded snapshot
  snapChecks(tag, d, 'own-compaction', c.update)
  for (const f of foreign || []) snapChecks(tag, d, f.label, f.bytes)
  for (const k of ['batch', 'batchLate']) {
    const bc = updateClients(unb64(d[k]))
    check(`${tag}:replica-numbers-in-${k === 'batch' ? 'initial' : 'incremental'}-batch`, same(bc, d[k + 'Clients']), `Yjs lists the numbers inside the ${k === 'batch' ? 'first' : 'later (clocks > 0)'} batch as [${bc}], author said [${d[k + 'Clients']}]`)
  }
  return { compacted: c.update }
}

function snapChecks(tag, d, label, bytes) {
  const doc = new Y.Doc()
  Y.applyUpdate(doc, bytes)
  const t = doc.getText('t')
  const p = `${tag}:snapshot[${label}]`
  check(`${p}:text`, t.toString() === d.finalText && !has(bytes, d.marker), `fresh Yjs doc loaded only from ${bytes.length} snapshot bytes reads the same text, deleted words absent from the bytes: ${!has(bytes, d.marker)}`)
  const l2 = resolve(t, doc, d.posLive)
  const l3 = resolve(t, doc, d.posDead)
  check(`${p}:old-position`, l2 === d.expectLive, `position made before the delete resolves to ${l2}, expected ${d.expectLive}`)
  check(`${p}:position-inside-deleted-text`, l3 === d.expectDead, `position inside the later-deleted words resolves to ${l3}, expected ${d.expectDead}`)
  const after = perLetter(t)
  check(`${p}:replica-per-letter`, same(after, d.clientsBefore), `letters keep their replica numbers: ${runs(after)}`)
}

// ---- replicas -----------------------------------------------------------
const NUMS = [1, 7, 2 ** 31, 2 ** 32 - 1, 2 ** 53 - 1]
function writeAs(n) {
  const d = new Y.Doc()
  d.clientID = n
  const t = d.getText('t')
  t.insert(0, 'x😀y')
  return { doc: d, update: Y.encodeStateAsUpdate(d) }
}
function reloadAndWrite(n, store) {
  // Yjs changes a doc's clientID when a remote update carries letters under it,
  // so the number is set AFTER the stored state is loaded (the naive order is
  // reported as its own check in replicasPhase1).
  const d = new Y.Doc()
  Y.applyUpdate(d, unb64(store))
  d.clientID = n
  const before = Y.decodeStateVector(Y.encodeStateVector(d)).get(n)
  const t = d.getText('t')
  t.insert(t.length, ' more🎉')
  const stored = new Y.Doc()
  Y.applyUpdate(stored, unb64(store))
  const delta = Y.encodeStateAsUpdate(d, Y.encodeStateVector(stored))
  return { doc: d, before, after: Y.decodeStateVector(Y.encodeStateVector(d)).get(n), delta }
}
function peerCheck(tag, n, store, delta, expectText) {
  const p = new Y.Doc()
  Y.applyUpdate(p, unb64(store))
  Y.applyUpdate(p, delta)
  const t = p.getText('t')
  const letters = perLetter(t)
  const ok = t.toString() === expectText && letters.every((x) => x === n)
  check(tag, ok, `peer holding the stored copy applies the delta: text ${JSON.stringify(t.toString())}, every letter under ${n}: ${letters.every((x) => x === n)}`)
}

function replicasPhase1() {
  const written = []
  for (const n of NUMS) {
    const w = writeAs(n)
    const got = perLetter(w.doc.getText('t'))
    check(`replica-set:yjs:${n}`, got.every((x) => x === n), `Yjs writes under ${n}: letters carry ${[...new Set(got)]}`)
    written.push({ n, update: b64(w.update) })
  }
  writeJson('js_r.json', { written })
  // stored replica reloaded in Yjs, same number, keeps writing
  const n = 4242
  const w = writeAs(n)
  const store = b64(w.update)
  const r = reloadAndWrite(n, store)
  check('replica-reload:yjs-self', r.after > r.before && r.before === 'x😀y'.length, `reloaded replica ${n} clock ${r.before} -> ${r.after}`)
  peerCheck('replica-reload:yjs-peer-converges', n, store, r.delta, 'x😀y more🎉')
  // naive order: number set first, stored state applied after
  const naive = new Y.Doc()
  naive.clientID = n
  Y.applyUpdate(naive, unb64(store))
  check('replica-reload:yjs-naive-order', null, `setting clientID=${n} and then applying the stored state: clientID is now ${naive.clientID === n ? 'still ' + n : 'CHANGED to ' + naive.clientID} (Yjs picks a new number when remote letters carry its own)`)
  // negative: reload without the stored state
  const lost = new Y.Doc()
  lost.clientID = n
  lost.getText('t').insert(0, 'Q')
  const p = new Y.Doc()
  Y.applyUpdate(p, unb64(store))
  Y.applyUpdate(p, Y.encodeStateAsUpdate(lost))
  check('replica-reload:yjs-without-state', null, `same number but no stored state: peer text is ${JSON.stringify(p.getText('t').toString())} (the new letter ${p.getText('t').toString().includes('Q') ? 'was kept' : 'was silently dropped'})`)
  writeJson('js_reload.json', { n, store, text: 'x😀y' })
}

// ---- weighing -----------------------------------------------------------
function rng(seed) {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}
const WORDS = ['analysis', 'the', 'design', 'plan', 'of', 'section', 'review', 'draft', 'and', 'with', 'cost', 'risk', '😀', 'café', 'step', 'owner']
function chunk(r, lo, hi) {
  let s = ''
  const target = lo + Math.floor(r() * (hi - lo))
  while (s.length < target) s += WORDS[Math.floor(r() * WORDS.length)] + ' '
  return s.slice(0, target).replace(/[\ud800-\udbff]$/, '')
}
const TARGET = 512 * 1024
function build(kind) {
  const r = rng(kind.length * 7919)
  const ids = [11, 22, 33, 44, 55, 66]
  const doc = new Y.Doc({ gc: true })
  const t = doc.getText('t')
  let ops = 0
  let bytes = 0
  const u8 = () => Buffer.byteLength(t.toString())
  const step = (op) => { doc.clientID = ids[Math.floor(r() * ids.length)]; op(); ops++ }
  if (kind === 'worst-single-letters') {
    // every letter its own edit at a random place, a fifth of them deleted again
    while (t.length < TARGET * 1.25) {
      doc.clientID = ids[Math.floor(r() * ids.length)]
      doc.transact(() => {
        for (let k = 0; k < 5000; k++) {
          const pos = Math.floor(r() * (t.length + 1))
          t.insert(pos, 'abcdefghij '[Math.floor(r() * 11)])
          ops++
          if (r() < 0.2) { t.delete(Math.floor(r() * t.length), 1); ops++ }
        }
      })
    }
    // trim to the cap with scattered deletes (text is ASCII here: bytes == units)
    while (t.length > TARGET) {
      const n = Math.min(2000, t.length - TARGET)
      doc.transact(() => { for (let k = 0; k < n; k++) { t.delete(Math.floor(r() * (t.length - 1)), 1); ops++ } })
    }
    return { doc, ops }
  }
  const churn = kind === 'churn'
  while (u8() < TARGET) {
    const x = r()
    step(() => {
      if (x < 0.7) t.insert(Math.floor(r() * (t.length + 1)), chunk(r, 5, 60))
      else if (x < 0.75) t.insert(Math.floor(r() * (t.length + 1)), 'x')
      else if (t.length > 50) {
        const at = Math.floor(r() * (t.length - 40))
        t.delete(at, 1 + Math.floor(r() * 10))
      }
    })
  }
  if (churn) {
    // keep the text near the cap but rewrite it several times over
    for (let i = 0; i < 60000; i++) {
      step(() => {
        const at = Math.floor(r() * (t.length - 80))
        const n = 5 + Math.floor(r() * 40)
        t.delete(at, n)
        t.insert(Math.floor(r() * (t.length + 1)), chunk(r, n, n + 1))
      })
    }
    while (u8() > TARGET) t.delete(t.length - 100, 100)
  } else {
    while (u8() > TARGET) t.delete(t.length - 100, 100)
  }
  return { doc, ops }
}
function weigh() {
  const rows = []
  for (const kind of ['typing', 'churn', 'worst-single-letters']) {
    const t0 = Date.now()
    const { doc, ops } = build(kind)
    const t = doc.getText('t')
    const text = t.toString()
    const t1 = Date.now()
    const upd = Y.encodeStateAsUpdate(doc)
    const fresh = new Y.Doc({ gc: true })
    Y.applyUpdate(fresh, upd)
    const compacted = Y.encodeStateAsUpdate(fresh)
    const v2 = Y.encodeStateAsUpdateV2(fresh)
    const t2 = Date.now()
    const textBytes = Buffer.byteLength(text)
    const row = {
      kind, ops, textBytes, textUnits: text.length,
      live: upd.length, compacted: compacted.length, deflated: zlib.deflateSync(compacted).length,
      v2: v2.length, v2deflated: zlib.deflateSync(v2).length,
      sha: crypto.createHash('sha256').update(text).digest('hex'),
      msBuild: t1 - t0, msCompact: t2 - t1,
    }
    rows.push(row)
    fs.writeFileSync(OUT + `weigh_${kind}.bin`, compacted)
    console.log('weigh', JSON.stringify(row))
  }
  writeJson('js_weigh.json', rows)
}

// ---- fuzz: many small multi-writer docs with mixed-width characters ---------
function fuzz() {
  const r = rng(12345)
  const ALPHA = ['a', 'b', ' ', 'é', 'ü', '中', '文', '😀', '🎉', '𝄞', 'x', '—']
  const pick = (n) => Array.from({ length: n }, () => ALPHA[Math.floor(r() * ALPHA.length)]).join('')
  const docs = []
  for (let k = 0; k < 40; k++) {
    const peers = [1000 + k * 10 + 1, 1000 + k * 10 + 2, 1000 + k * 10 + 3].map((id) => {
      const d = new Y.Doc()
      d.clientID = id
      return d
    })
    const sync = () => {
      for (const a of peers) for (const b of peers) if (a !== b) Y.applyUpdate(b, Y.encodeStateAsUpdate(a))
    }
    for (let round = 0; round < 6; round++) {
      for (const d of peers) {
        const t = d.getText('t')
        for (let e = 0; e < 3; e++) {
          if (t.length > 6 && r() < 0.3) {
            // delete whole characters only
            const s = t.toString()
            const cps = Array.from(s)
            const from = Math.floor(r() * (cps.length - 1))
            const n = 1 + Math.floor(r() * 3)
            const start = cps.slice(0, from).join('').length
            const len = cps.slice(from, from + n).join('').length
            t.delete(start, len)
          } else {
            const s = t.toString()
            const cps = Array.from(s)
            const at = cps.slice(0, Math.floor(r() * (cps.length + 1))).join('').length
            t.insert(at, pick(1 + Math.floor(r() * 5)))
          }
        }
      }
      if (round % 2 === 1) sync()
    }
    sync()
    const t = peers[0].getText('t')
    const text = t.toString()
    const cps = Array.from(text)
    const positions = []
    for (let p = 0; p < 12 && cps.length; p++) {
      const idx = cps.slice(0, Math.floor(r() * cps.length)).join('').length
      positions.push({ idx, pos: b64(Y.encodeRelativePosition(Y.createRelativePositionFromTypeIndex(t, idx))) })
    }
    docs.push({ update: b64(Y.encodeStateAsUpdate(peers[0])), text, clients: perLetter(t), positions })
  }
  writeJson('js_fuzz.json', docs)
  console.log('fuzz docs written:', docs.length, 'e.g.', JSON.stringify(docs[0].text))
}

// ---- phases ---------------------------------------------------------------
if (phase === '1') {
  const d = author(101, 102)
  writeJson('js_a.json', d)
  console.log('final text:', JSON.stringify(d.finalText), 'letters:', runs(d.clientsBefore))
  const v = verify('yjs-reads-yjs', d)
  fs.writeFileSync(OUT + 'snap_yjs_of_js.b64', b64(v.compacted))
  replicasPhase1()
  fuzz()
  if (process.argv[3] !== 'noweigh') weigh()
  writeJson('results_node1.json', results)
} else if (phase === '2') {
  const d = readJson('py_a.json')
  console.log('pycrdt-authored text:', JSON.stringify(d.finalText))
  const pySnap = unb64(fs.readFileSync(OUT + 'snap_pycrdt_of_py.b64', 'utf8'))
  const v = verify('yjs-reads-pycrdt', d, [{ label: 'pycrdt-compaction', bytes: pySnap }])
  fs.writeFileSync(OUT + 'snap_yjs_of_py.b64', b64(v.compacted))
  // pycrdt-written replica numbers
  const pr = readJson('py_r.json')
  for (const e of pr.written) {
    if (e.error) { check(`replica-set:pycrdt-read-in-yjs:${e.n}`, false, `pycrdt could not write under ${e.n}: ${e.error}`); continue }
    const doc = new Y.Doc()
    Y.applyUpdate(doc, unb64(e.update))
    const t = doc.getText('t')
    const got = perLetter(t)
    check(`replica-set:pycrdt-read-in-yjs:${e.n}`, got.every((x) => x === e.n) && t.toString() === 'x😀y', `Yjs reads pycrdt's letters as ${[...new Set(got)]}, text ${JSON.stringify(t.toString())}`)
  }
  // pycrdt's reloaded replica: its delta applies on a Yjs peer holding its stored copy
  const rl = readJson('py_reload.json')
  peerCheck('replica-reload:pycrdt-writes-read-in-yjs', rl.n, rl.store, unb64(rl.delta), 'x😀y more🎉')
  const r = reloadAndWrite(rl.n, rl.store)
  check('replica-reload:yjs-reloads-pycrdt-store', r.after > r.before, `Yjs reloads the pycrdt store under ${rl.n}: clock ${r.before} -> ${r.after}`)
  peerCheck('replica-reload:yjs-reloads-pycrdt-store-peer', rl.n, rl.store, r.delta, 'x😀y more🎉')
  writeJson('results_node2.json', results)
}
