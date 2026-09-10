import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.experiment_manifest import build_manifest, validate_manifest, write_new_manifest, create_frozen_experiment


class ExperimentManifestTests(unittest.TestCase):
    def test_hash_validation_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=root/'config.yaml';uni=root/'universe.csv';data=root/'bars.csv'
            cfg.write_text('x: 1');uni.write_text('code\nUS.X\n');data.write_text('date\n2026-01-01\n')
            periods={'development_end':'2020-12-31','validation_start':'2021-01-01',
                     'validation_end':'2023-12-31','test_start':'2024-01-01'}
            with patch('scripts.experiment_manifest.git_commit',return_value='abc'), \
                 patch('scripts.experiment_manifest.git_is_dirty',return_value=False):
                m=build_manifest('E1',cfg,uni,[data],periods,root=root)
                self.assertEqual(validate_manifest(m,root),[])
                write_new_manifest(root/'out',m)
                with self.assertRaises(FileExistsError):write_new_manifest(root/'out',m)
                data.write_text('changed')
                self.assertIn('DATA_HASH_MISMATCH',validate_manifest(m,root))

    def test_create_copies_frozen_small_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=root/'c.yaml';uni=root/'u.csv';data=root/'d.csv'
            cfg.write_text('x: 1');uni.write_text('code\nUS.X');data.write_text('x')
            periods={'development_end':'2020','validation_start':'2021',
                     'validation_end':'2023','test_start':'2024'}
            with patch('scripts.experiment_manifest.git_commit',return_value='abc'), \
                 patch('scripts.experiment_manifest.git_is_dirty',return_value=False):
                path=create_frozen_experiment(root/'exp','E1',cfg,uni,[data],periods,root)
            self.assertTrue(path.exists());self.assertTrue((root/'exp'/'config.yaml').exists())
            self.assertTrue((root/'exp'/'universe.csv').exists())


if __name__=='__main__':unittest.main()
