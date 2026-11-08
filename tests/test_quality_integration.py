"""Tests specific to the integration with the original v0.5.1 app."""
import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
loader = importlib.machinery.SourceFileLoader('app_quality_module', str(ROOT / 'app.pyw'))
spec = importlib.util.spec_from_loader(loader.name, loader)
app = importlib.util.module_from_spec(spec)
loader.exec_module(app)


class IntegrationTests(unittest.TestCase):
    def test_coverage_high_confidence_counter_only(self):
        info = {'chen_qianyu_(arknights)': {'exists': True, 'category': 4}}
        final_tags = ['chen_qianyu_(arknights)', '2girls', '4boys', 'multiple_girls']
        # HIGH only contains 2girls; conflicting LOWEST predictions must not
        # inflate the warning to >=6 subjects.
        result = app.find_character_coverage(final_tags, info, count_tags=['2girls'])
        self.assertEqual(result['minimum_subjects'], 2)
        self.assertEqual(result['recognized_characters'], 1)

    def test_conflicting_reliable_counters_do_not_invent_missing_people(self):
        info = {'a_(fiction)': {'exists': True, 'category': 4}}
        tags = ['a_(fiction)', '2girls', '1girl']
        self.assertIsNone(app.find_character_coverage(tags, info, count_tags=['2girls', '1girl']))

    def test_one_character_two_views_no_missing_warning(self):
        info = {'a_(fiction)': {'exists': True, 'category': 4}}
        tags = ['a_(fiction)', 'multiple_views', '2girls']
        self.assertIsNone(app.find_character_coverage(tags, info, count_tags=['2girls']))

    def test_existing_coverage_behavior(self):
        info = {'a_(fiction)': {'exists': True, 'category': 4}}
        result = app.find_character_coverage(['a_(fiction)', '6+girls'], info)
        self.assertEqual(result['minimum_subjects'], 6)

    def test_invalid_image_rejected_before_any_model_work(self):
        fake = MagicMock()
        fake.busy = False
        fake.image_path = None
        with patch.object(app.messagebox, 'showerror') as error_dialog:
            app.DanbooruTaggerApp.analyze(fake)
        error_dialog.assert_called_once()
        fake.analyze_btn.configure.assert_called_once_with(state='disabled')
        fake._load_model.assert_not_called()
        fake.clear_outputs.assert_not_called()

    def test_status_progress_have_separate_layout_rows(self):
        # Parse the actual main-window initializer: this test runs headlessly.
        import ast
        source = (ROOT / 'app.pyw').read_text(encoding='utf-8')
        tree = ast.parse(source)
        app_class = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                         and n.name == 'DanbooruTaggerApp')
        init = next(n for n in app_class.body if isinstance(n, ast.FunctionDef)
                    and n.name == '__init__')
        init_source = ast.get_source_segment(source, init)
        self.assertIn('self.status_label.grid(row=0, column=0', init_source)
        self.assertIn('self.progress.grid(row=1, column=0', init_source)
        self.assertIn('self.status_label.configure(wraplength=', init_source)

    def test_existing_model_and_bucket_thresholds_unchanged(self):
        self.assertEqual(app.MODEL_ID, 'pixai-labs/pixai-tagger-v1.0')
        buckets = app.split_by_confidence([('standing', .2), ('girl', .9)], .75, .45, .17)
        self.assertEqual(buckets, {'high': ['girl'], 'medium': [], 'low': ['standing']})


if __name__ == '__main__':
    unittest.main()
