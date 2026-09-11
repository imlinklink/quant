import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.experiment_manifest import build_manifest, validate_manifest, write_new_manifest, create_frozen_experiment


class ExperimentManifestTests(unittest.TestCase):
    def test_hash_validation_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=root/'config.yaml';uni=root/'universe.csv';data=root/'bars.csv';quality=root/'quality.csv'
            cfg.write_text('x: 1');uni.write_text('code\nUS.X\n');data.write_text('date\n2026-01-01\n');quality.write_text('quality\ngood\n')
            periods={'development_end':'2020-12-31','validation_start':'2021-01-01',
                     'validation_end':'2023-12-31','test_start':'2024-01-01'}
            with patch('scripts.experiment_manifest.git_commit',return_value='abc'), \
                 patch('scripts.experiment_manifest.git_is_dirty',return_value=False):
                m=build_manifest('E1',cfg,uni,[data],periods,[quality],root=root)
                self.assertEqual(validate_manifest(m,root),[])
                write_new_manifest(root/'out',m)
                with self.assertRaises(FileExistsError):write_new_manifest(root/'out',m)
                data.write_text('changed')
                self.assertIn('DATA_HASH_MISMATCH',validate_manifest(m,root))

    def test_create_copies_frozen_small_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=root/'c.yaml';uni=root/'u.csv';data=root/'d.csv';quality=root/'q.csv'
            cfg.write_text('x: 1');uni.write_text('code\nUS.X');data.write_text('x');quality.write_text('good')
            periods={'development_end':'2020','validation_start':'2021',
                     'validation_end':'2023','test_start':'2024'}
            with patch('scripts.experiment_manifest.git_commit',return_value='abc'), \
                 patch('scripts.experiment_manifest.git_is_dirty',return_value=False):
                path=create_frozen_experiment(root/'exp','E1',cfg,uni,[data],periods,root,[quality])
            self.assertTrue(path.exists());self.assertTrue((root/'exp'/'config.yaml').exists())
            self.assertTrue((root/'exp'/'universe.csv').exists())


    def test_experiment_groups_freeze_and_llm_inconclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=root/'config.yaml';uni=root/'universe.csv';data=root/'bars.csv';quality=root/'quality.csv'
            cfg.write_text('x: 1');uni.write_text('code\nUS.X\n');data.write_text('date\n2026-01-01\n');quality.write_text('quality\ngood\n')
            periods={'development_end':'2020-12-31','validation_start':'2021-01-01',
                     'validation_end':'2023-12-31','test_start':'2024-01-01'}
            with patch('scripts.experiment_manifest.git_commit',return_value='abc'), \
                 patch('scripts.experiment_manifest.git_is_dirty',return_value=False):
                m=build_manifest('BUY-WD-ABC-EXP-001',cfg,uni,[data],periods,[quality],
                                 root=root,experiment_groups=('A','B','C'))
                self.assertEqual(m['experiment_groups'],['A','B','C'])
                self.assertEqual(m['llm_evaluation'],
                                 {'status':'inconclusive',
                                  'reason':'historical_point_in_time_labels_unavailable'})
                self.assertEqual(validate_manifest(m,root),[])
                bad=dict(m);bad['experiment_groups']=['A','C','B']
                self.assertIn('EXPERIMENT_GROUPS_INVALID',validate_manifest(bad,root))
                bad2=dict(m);bad2['experiment_groups']=['A','X']
                self.assertIn('EXPERIMENT_GROUPS_INVALID',validate_manifest(bad2,root))
                bad3=dict(m);bad3['llm_evaluation']={'status':'evaluated','reason':''}
                self.assertIn('LLM_EVALUATION_MUST_BE_INCONCLUSIVE_WITHOUT_D',
                              validate_manifest(bad3,root))


if __name__=='__main__':unittest.main()
