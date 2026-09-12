"""证据快照 CLI 端到端冒烟测试（§3.4/§3.5）。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]

ROW = {'security_id': 'SEC-A', 'symbol_as_published': 'US.A', 'kind': 'earnings',
       'source_id': 'src1', 'source_record_id': 'r1', 'source_url_or_archive_path': 'archive://1',
       'event_at': '2022-02-01T00:00:00Z', 'published_at': '2022-03-09T21:05:00Z',
       'observed_at': '2022-03-09T21:06:00Z', 'ingested_at': '2026-09-12T00:00:00Z',
       'version_id': 'v1', 'supersedes_id': '', 'content_hash': 'body-v1', 'summary_hash': 's1',
       'quality_status': 'verified', 'availability_proof': 'snapshot',
       'license_tag': 'research-retention-allowed'}


def run(args):
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)


class EvidenceCliTests(unittest.TestCase):
    def test_diagnostic_packets_cannot_be_replayed_as_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / 'packets' / 'packets').mkdir(parents=True)
            (tmp / 'packets' / 'packets' / 's1.json').write_text(json.dumps({
                'packet_hash': 'pkt_x', 'decision_cutoff': '2022-03-10T21:00:00Z',
                'evidence_mode': 'diagnostic', 'events': []}))
            result = run(['scripts/evidence/replay_historical_selection.py',
                          '--packets', str(tmp / 'packets'), '--output-dir', str(tmp / 'job')])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('STRICT_REPLAY_REQUIRES_STRICT_PACKETS', result.stderr)

    def test_import_audit_packet_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / 'source').mkdir()
            pd.DataFrame([ROW, dict(ROW, source_record_id='r2', content_hash='body-v2')]).to_csv(
                tmp / 'source' / 'evidence.csv', index=False)

            imported = run(['scripts/evidence/import_historical_evidence.py',
                            '--source', str(tmp / 'source'), '--output-dir', str(tmp / 'store')])
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertTrue((tmp / 'store' / 'evidence.jsonl').is_file())

            audited = run(['scripts/evidence/audit_historical_evidence.py',
                           '--evidence', str(tmp / 'store' / 'evidence.jsonl'),
                           '--output-dir', str(tmp / 'quality')])
            self.assertEqual(audited.returncode, 0, audited.stderr)
            quality = json.loads((tmp / 'quality' / 'source_quality.json').read_text())
            self.assertTrue(quality['passed'])
            self.assertTrue(quality['retention_allowed'])

            pd.DataFrame([{'setup_id': 's1', 'security_id': 'SEC-A',
                           'decision_cutoff': '2022-03-10T21:00:00Z'}]).to_csv(
                tmp / 'setups.csv', index=False)
            built = run(['scripts/evidence/build_historical_packet.py',
                         '--setups', str(tmp / 'setups.csv'),
                         '--evidence', str(tmp / 'store' / 'evidence.jsonl'),
                         '--output-dir', str(tmp / 'packets')])
            self.assertEqual(built.returncode, 0, built.stderr)
            packet = json.loads((tmp / 'packets' / 'packets' / 's1.json').read_text())
            self.assertEqual(len(packet['events']), 2)          # 两个不同事件

            # 不给标签：只产出待标注清单，不生成标签
            replay = run(['scripts/evidence/replay_historical_selection.py',
                          '--packets', str(tmp / 'packets'), '--output-dir', str(tmp / 'job')])
            self.assertEqual(replay.returncode, 0, replay.stderr)
            job = json.loads((tmp / 'job' / 'job.json').read_text())
            self.assertEqual(len(job), 1)

            # 给合法标签：校验通过
            inside = packet['events'][0]['evidence_id']
            labels = tmp / 'labels.jsonl'
            labels.write_text(json.dumps({'setup_id': 's1', 'packet_hash': packet['packet_hash'],
                                          'llm_decision': 'candidate',
                                          'cited_evidence_ids': [inside]}) + '\n')
            replayed = run(['scripts/evidence/replay_historical_selection.py',
                            '--packets', str(tmp / 'packets'), '--labels', str(labels),
                            '--output-dir', str(tmp / 'replay')])
            self.assertEqual(replayed.returncode, 0, replayed.stderr)
            coverage = json.loads((tmp / 'replay' / 'coverage.json').read_text())
            self.assertEqual(coverage['coverage'], 1.0)

            # 越界引用：校验失败
            bad = tmp / 'bad.jsonl'
            bad.write_text(json.dumps({'setup_id': 's1', 'packet_hash': packet['packet_hash'],
                                       'llm_decision': 'candidate',
                                       'cited_evidence_ids': ['ev_nope']}) + '\n')
            failed = run(['scripts/evidence/replay_historical_selection.py',
                          '--packets', str(tmp / 'packets'), '--labels', str(bad),
                          '--output-dir', str(tmp / 'replay_bad')])
            self.assertNotEqual(failed.returncode, 0)


if __name__ == '__main__':
    unittest.main()
