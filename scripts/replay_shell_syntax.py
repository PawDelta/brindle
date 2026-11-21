"""Read-only replay of historical shell-syntax asks; never execute their commands.

DB summaries are truncated. Recover full requests from pending records and Claude
transcripts, matching tool-use IDs (or an unambiguous summary). Missing commands
are reported separately, never replayed as truncated shell source. Debug logs are
counted for provenance; they may already have rotated out of the requested window.

Run with PYTHONPATH=src and the worktree's Python. --end requires an ISO timestamp
with an offset. Results use current local rules, no saved grants or org lookups.
Filesystem-dependent decisions reflect the files present at replay time.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import subprocess

from brindle import permissions as p


def json_lines(path):
    try:
        with path.open() as stream:
            for line in stream:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def recover(db, rows, roots, start, end):
    requests = {}
    by_id = {row['request_id']: row['id'] for row in rows if row['request_id']}
    by_summary = defaultdict(set)
    for row in rows:
        by_summary[row['summary']].add(row['id'])
    candidates = defaultdict(dict)
    for row in db.execute('SELECT agent_id, tool_use_id, request FROM permission_requests'):
        if row['tool_use_id'] in by_id:
            requests[by_id[row['tool_use_id']]] = p.Request.from_dict(json.loads(row['request']))
    for root in roots:
        for path in root.glob('*/*.jsonl'):
            if path.stat().st_mtime < start:
                continue
            for entry in json_lines(path):
                stamp = entry.get('timestamp')
                try:
                    ts = datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
                except (TypeError, ValueError, AttributeError):
                    continue
                if not start <= ts < end or entry.get('type') != 'assistant':
                    continue
                content = entry.get('message', {}).get('content', [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if block.get('type') != 'tool_use' or block.get('name') != 'Bash':
                        continue
                    command = block.get('input', {}).get('command')
                    if not isinstance(command, str):
                        continue
                    req = p.Request('claude', 'bash', 'Bash', cwd=entry.get('cwd', ''), command=command)
                    if block.get('id') in by_id:
                        requests[by_id[block['id']]] = req
                    for row_id in by_summary.get(req.summary(), ()):
                        candidates[row_id][command, req.cwd] = req
    for row in rows:
        if row['id'] not in requests and len(candidates[row['id']]) == 1:
            requests[row['id']] = next(iter(candidates[row['id']].values()))
    return requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home() / '.brindle')
    parser.add_argument('--end', required=True)
    parser.add_argument('--transcripts', type=Path, action='append', default=[])
    parser.add_argument('--details', action='store_true', help='Print commands; may contain private paths')
    parser.add_argument('--include-summaries', action='store_true',
                        help='Also replay complete DB summaries (whitespace normalized, cwd may be missing)')
    parser.add_argument('--base', help='Replay the permissions source at this Git revision')
    args = parser.parse_args()
    end = datetime.fromisoformat(args.end)
    if end.tzinfo is None:
        parser.error('--end must include a UTC offset')
    if args.base:
        source = subprocess.run(['git', 'show', f'{args.base}:src/brindle/permissions.py'],
                                check=True, capture_output=True, text=True).stdout
        # Keep the canonical module: permission_groups imports it during decisions.
        # No historical shell command is executed, only this repository's checker.
        exec(compile(source, f'{args.base}:src/brindle/permissions.py', 'exec'), p.__dict__)
    start = end - timedelta(days=7)
    db = sqlite3.connect(f'{(args.home / "brindle.db").resolve().as_uri()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM permission_asks WHERE ts >= ? AND ts < ? "
                      "AND kind='bash' AND reason LIKE '%shell syntax cannot be checked safely%' ORDER BY id",
                      (start.timestamp(), end.timestamp())).fetchall()
    roots = args.transcripts or [Path.home() / '.claude/projects',
                                Path.home() / 'Library/Application Support/brindle/config/claude/projects']
    requests = recover(db, rows, roots, start.timestamp(), end.timestamp())
    db.close()
    debug = Counter()
    for name in ('debug.log.1', 'debug.log'):
        for entry in json_lines(args.home / 'debug' / name):
            if start.timestamp() <= entry.get('ts', 0) < end.timestamp():
                debug[entry.get('source', 'unknown')] += 1
    store = json.loads((args.home / 'permissions.json').read_text())
    rules = [rule for data in store.get('rules', []) if (rule := p.Rule.from_dict(data))]
    counts = Counter()
    sources = Counter()
    for row in rows:
        req = requests.get(row['id'])
        source = 'full request'
        if req is None and args.include_summaries and not row['summary'].endswith('…'):
            tool, sep, command = row['summary'].partition(': ')
            if sep:
                req = p.Request('claude', 'bash', tool, cwd=row['repo_root'], command=command)
                source = 'normalized summary'
        if req is None:
            counts['unrecoverable'] += 1
            if args.details:
                print(json.dumps({'id': row['id'], 'decision': 'unrecoverable', 'summary': row['summary']}))
            continue
        sources[source] += 1
        req = p.replace(req, repo_root=row['repo_root'], worktree=req.cwd)
        if p.prints_credentials(req):
            decision = p.Decision('deny', 'credential read')
        else:
            decision = p._decide(req, checks=None, rules=rules, unmatched=store.get('unmatched', 'allow'),
                                 session=None, saved=[], spend=False, groups=store.get('groups', {}))
        syntax = decision.decision == 'ask' and any(r and r.id == p.SHELL_SYNTAX_RULE
                                                   for r in (decision.rule, *decision.asks))
        category = 'syntax ask' if syntax else decision.decision
        counts[category] += 1
        why = []
        p.read_parts(req, rules, why=why)
        if args.details:
            print(json.dumps({'id': row['id'], 'decision': category, 'why': [str(e) for e in why],
                              'command': req.command, 'cwd': req.cwd}))
    print(json.dumps({'start': start.isoformat(), 'end': end.isoformat(), 'historical': len(rows),
                      'counts': counts, 'sources': sources, 'debug_sources': debug}, sort_keys=True))


if __name__ == '__main__':
    main()
