---
name: security/backend
description: Backend code handles untrusted input, secrets and the shell carefully
deny_patterns:
  - \bshell\s*=\s*(?!(?:False|None|0)\b)\S
  - \bos\.(?:system|popen|execl?p?e?|spawnl?p?e?)\s*\(
  - \beval\s*\(
  - \bexec\s*\(
  - \bpickle\.loads?\s*\(
  - \byaml\.load\s*\((?![^)]*Loader)
  - \bverify\s*=\s*(?:False|0)\b
  - (?i)\b(password|passwd|secret|api_key|apikey|token)\s*[=:]\s*['"][^'"]{8,}['"]
---
Treat every input that crosses a trust boundary (requests, files, environment,
messages from other processes) as hostile until validated. Never build a shell
command from it: pass arguments as a list, never `shell=True`, and never
`eval`/`exec` on anything a user could influence. Don't deserialize untrusted
data with pickle or `yaml.load` without a safe loader. Don't turn off TLS
verification. Keep secrets out of source: read them from the environment or a
secret store, never a literal in code, a test fixture, or a log line. Use
parameterized queries, never string-built SQL. Prefer the standard library's
hashing and random primitives for anything security-relevant (`secrets`, not
`random`; no MD5/SHA-1 for integrity or passwords). If a change widens what
the service accepts or exposes, say so in your report.
