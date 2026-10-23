| Task | Issue | Branch | Status (done / blocked / skipped) | Failing-before test | Full suite | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| T5 | #261 | `fix/database-schema-version` | done | `tests/test_watch.py::test_db_refuses_newer_schema_before_changing_it`: opened silently before; passes after. Extended existing migration test also failed before because the stored version remained 0. | Deferred to supervisor after Phase 1. | Store SQLite `user_version` after migrations; reject newer schemas before schema writes. Daemon behavior and T17 remain unchanged. |
