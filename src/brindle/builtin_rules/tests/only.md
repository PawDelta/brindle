---
name: tests/only
description: Change tests, not the code under test
require_tests_for:
  - "*"
---
You are here to write, extend or fix tests. Do not change the code under test:
if a test can't pass without a production change, don't make that change;
write the failing test, say precisely what the code would need, and report.
Keep each test independent, named for the behaviour it pins down, and free of
sleeps or ordering assumptions. Prefer the project's existing fixtures and
helpers over new ones.
