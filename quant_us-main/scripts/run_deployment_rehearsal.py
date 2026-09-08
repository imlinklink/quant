"""Offline acceptance: copy historical logs, then run temporary-DB/mock tests.

No broker connection or migration of the runtime database is performed.
Run from the project root; output contains private history and stays under data/.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
import xml.etree.ElementTree as ET


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    root = Path(__file__).resolve().parents[1]
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = root / 'data' / 'deployment-rehearsal' / stamp
    output.mkdir(parents=True, exist_ok=False)
    snapshots = []
    for name in ('approvals/decisions.jsonl', 'decision_ledger/signals.jsonl'):
        source = root / 'data' / name
        if not source.exists():
            snapshots.append(dict(file=name, status='missing'))
            continue
        data = source.read_bytes()
        target = output / 'history' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        invalid = 0
        lines = [line for line in data.splitlines() if line.strip()]
        for line in lines:
            try:
                json.loads(line)
            except (ValueError, UnicodeError):
                invalid += 1
        snapshots.append(dict(file=name, sha256=sha(data), lines=len(lines),
                              invalid_lines=invalid, copy_verified=sha(target.read_bytes()) == sha(data)))
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', 'tests/unit', '-q', '--disable-warnings',
         '--junitxml=' + str(output / 'tests.xml')], cwd=root,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    (output / 'tests.log').write_text(result.stdout)
    for entry in snapshots:
        if 'sha256' in entry:
            entry['source_unchanged'] = sha((root / 'data' / entry['file']).read_bytes()) == entry['sha256']
    totals = {}
    if (output / 'tests.xml').exists():
        suites = ET.parse(output / 'tests.xml').getroot().iter('testsuite')
        for suite in suites:
            for key in ('tests', 'failures', 'errors', 'skipped'):
                totals[key] = totals.get(key, 0) + int(suite.get(key, '0'))
    report = dict(created_at=datetime.now(timezone.utc).isoformat(),
                  runtime_db_exists=(root / 'data/execution.sqlite3').exists(),
                  runtime_db_migration='not_performed',
                  migration_exercised='synthetic legacy books schema in temporary databases',
                  broker='mock only; no OpenD connection',
                  history=snapshots, pytest_exit_code=result.returncode, test_totals=totals)
    report['passed'] = result.returncode == 0 and all(
        entry.get('copy_verified') and entry.get('source_unchanged') and not entry.get('invalid_lines')
        for entry in snapshots)
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(dict(report=str(output / 'report.json'), **report), ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
