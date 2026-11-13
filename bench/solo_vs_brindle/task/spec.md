# Feature request: richer querying, constraints and change notifications for TinyDB tables

You are working in a checkout of [TinyDB](https://github.com/msiemens/tinydb), the
small document database written in pure Python. Its tables (`tinydb.table.Table`)
can insert, search, update and remove documents, but users keep asking for
things they end up writing by hand on top of `search()`: sorted and paged
results, returning only some fields, simple statistics, "this field must be
unique", and a way to react when data changes.

Implement the five parts below. They are separable, but they share two pieces
of machinery, so expect to build those once:

- **field paths** (used by parts 1–4), and
- **the table's write path** (parts 4 and 5 both hook into every write).

Everything described here is public API and must work both on a `Table`
obtained from `db.table(name)` and on the database's default table through the
`TinyDB` instance itself (for example `db.find(...)`), the same way existing
methods such as `db.search(...)` do. It must work with every storage TinyDB
ships (`MemoryStorage`, `JSONStorage`, and either wrapped in
`CachingMiddleware`). Existing behaviour and the existing test suite must keep
working unchanged.

Add tests for your work to the repository's own test suite, and keep the code
style of the surrounding code (the project runs `pycodestyle`).

---

## Shared concept: field paths

Several methods below take a **field path**: a string naming a field of a
document. Dots separate the keys of nested mappings:

- `"name"` is the top-level key `name`;
- `"address.city"` is `doc["address"]["city"]`.

A path **resolves** if every key along it exists and every intermediate value
is a mapping. Otherwise the field is **missing** from that document (for
example, `"address.city"` is missing from `{"address": "Main St 1"}` and from
`{}`). Lists are never traversed: there is no index syntax.

A field whose value is `None` is **present** but **null**. Where the parts
below say "missing or null", both cases are treated the same way.

---

## Part 1 — `Table.find`: filtering, ordering and pagination

Add a method

```python
Table.find(cond=None, *, order_by=None, skip=0, limit=None, fields=None) -> list[Document]
```

(`fields` is Part 2.)

- `cond` is an ordinary TinyDB query (anything `search()` accepts). With
  `cond=None`, every document in the table is considered.
- Each result is a `tinydb.table.Document` whose `doc_id` is the document's
  integer ID, exactly as `search()` returns them, regardless of storage.
- Without `order_by`, results come in the table's normal order (the same order
  `all()` uses).
- `order_by` is either one sort key or a list/tuple of sort keys. A sort key is
  a field path, sorted ascending, or a field path prefixed with `-`, sorted
  descending (for example `"-age"`). With several keys, the first key decides
  and later keys break ties.
- Documents for which a sort key's field is missing or null sort **after** all
  documents that have a value for it, **in both directions**.
- Sorting is stable: documents that compare equal on every sort key keep their
  relative table order, in both directions.
- Values are compared with Python's normal ordering. If two values that must be
  compared are not comparable (for example a string and a number), `find`
  raises `TypeError`.
- `skip` (default `0`) drops that many results from the front, and `limit`
  (default `None`, meaning no limit) caps how many are returned. Both are
  applied after filtering and ordering. A negative `skip` or `limit` raises
  `ValueError`. `limit=0` returns an empty list.
- Documents returned by `find` are independent copies: modifying a returned
  document, including values nested inside it, never changes what is stored in
  the database.

Example:

```python
>>> db.find(where("team") == "red", order_by=["-score", "name"], limit=2)
[{'name': 'Ann', 'team': 'red', 'score': 9}, {'name': 'Bo', 'team': 'red', 'score': 7}]
```

## Part 2 — projection with `find(fields=...)`

`fields` is `None` (return whole documents) or a list/tuple of field paths.

- With `fields`, each returned `Document` contains only the listed fields, and
  keeps its `doc_id`.
- A nested path keeps its nesting: projecting `"address.city"` out of
  `{"name": "Ann", "address": {"city": "Oslo", "zip": "0150"}}` gives
  `{"address": {"city": "Oslo"}}`. Several paths under the same parent are
  merged into one nested mapping.
- A listed field that is missing from a document is simply left out of that
  result (the document is still returned, possibly as `{}`). A field whose
  value is `None` is present and is included.
- Projection only shapes the output: filtering and `order_by` still see the
  whole stored document, so you can sort on a field you do not project.
- Projected documents are independent copies, as in Part 1.

## Part 3 — aggregation

Add three methods. In all of them `cond` is an optional query restricting
which documents take part (`None` means all documents), and documents where the
field is missing or null are ignored unless stated otherwise.

### `Table.distinct(path, cond=None) -> list`

The distinct values of the field, in the order they are first seen in the
table. Values are compared by equality, so unhashable values such as lists and
dicts must work too.

### `Table.aggregate(path, op, cond=None)`

`op` is one of the strings `"count"`, `"sum"`, `"avg"`, `"min"`, `"max"`.

- `"count"`: how many documents have a non-null value for the field.
- `"sum"`: the sum of the values; `0` when there are none.
- `"avg"`: the arithmetic mean as a `float`; `None` when there are none.
- `"min"` / `"max"`: the smallest / largest value under Python ordering;
  `None` when there are none.
- `"sum"` and `"avg"` only accept numbers (`int` and `float`). Any other value,
  **including `True`/`False`**, raises `TypeError`.
- An unknown `op` raises `ValueError`, even when the table is empty.

### `Table.group_by(path, cond=None) -> dict`

Maps each distinct value of the field to the list of documents (`Document`
objects with their `doc_id`) that have that value, in table order. Documents
whose field is missing or null are **not** ignored here: they are grouped under
the key `None`. The dictionary's keys appear in the order the values are first
seen. If a value cannot be used as a dictionary key (for example a list),
`group_by` raises `TypeError`.

## Part 4 — unique constraints

Add `Table.ensure_unique(path)` and an exception class `UniqueViolation`,
importable as `from tinydb import UniqueViolation`, which is a subclass of
`ValueError`.

- `ensure_unique(path)` declares that no two documents in the table may have
  equal values for the field. Calling it again for the same path does nothing.
  A table can have several unique fields; each is checked on its own.
- Documents where the field is missing or null are exempt: any number of them
  may coexist.
- Values are compared by equality (so `1` and `1.0` are the same value), and
  may be of any type a document can hold, including lists and dicts.
- If the documents already in the table violate the constraint,
  `ensure_unique` raises `UniqueViolation` and the constraint is not added.
- Once declared, every write that would leave two documents with equal values
  raises `UniqueViolation`. This covers `insert`, `insert_multiple` (including
  two clashing documents within the same batch), `update` (with a mapping or a
  callable, by query, by `doc_ids`, or on all documents), `update_multiple` and
  `upsert`. Updating a document to the value it already has is not a violation.
- A write that raises `UniqueViolation` leaves the table exactly as it was
  before the call: no partially inserted batch and no partially applied update,
  with every storage, including `MemoryStorage`.
- Constraints belong to the `Table` object (and therefore last as long as the
  `TinyDB` instance hands out that table). They are not written to storage.

## Part 5 — change notifications

Add `Table.watch(callback)`, which registers `callback` to be told about every
change to that table's documents, and returns a function that unregisters it.
Calling that function more than once is harmless.

The callback is called as `callback(op, doc_id, old, new)`:

| `op`       | when                                   | `old`              | `new`              |
|------------|----------------------------------------|--------------------|--------------------|
| `"insert"` | a document was added                    | `None`             | the new contents   |
| `"update"` | an existing document's contents changed | contents before    | contents after     |
| `"remove"` | a document was deleted                  | contents before    | `None`             |

- `doc_id` is the document's integer ID; `old` and `new` are plain `dict`
  copies. Mutating them never changes the database.
- Every write method reports through this: `insert`, `insert_multiple`,
  `update`, `update_multiple`, `upsert`, `remove`, and `truncate` (which
  reports a `"remove"` for each document it deletes).
- There is exactly one call per affected document, in table order (newly
  inserted documents in the order they were inserted).
- An update only reports documents whose contents actually changed: setting a
  field to the value it already has produces no `"update"` call.
- Callbacks run only after the change has been stored. When a callback runs,
  every way of reading the table (including a repeated `search()` with a query
  that was run before the write) already sees the new state.
- A write that fails (for example with `UniqueViolation`, because
  `insert_multiple` was given something that is not a mapping, or because a
  callable passed to `update` raised part way through the matching documents)
  changes nothing and reports nothing. This holds for every storage, including
  `MemoryStorage`, whenever the table has at least one watcher.
- Several callbacks may watch the same table; each gets every change, in the
  order they were registered. A callback only hears about its own table.
