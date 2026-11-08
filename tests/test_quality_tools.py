import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from quality_tools import (AnalyzeButtonController, BlacklistStore,
                           estimate_character_count, export_confidences,
                           normalize_scores, validate_image_path)


class FakeVar:
    def __init__(self, value=''):
        self.value, self.fn = value, None
    def get(self):
        return self.value
    def set(self, value):
        self.value = value
        if self.fn:
            self.fn('', '', '')
    def trace_add(self, mode, fn):
        self.fn = fn
        return 'trace1'
    def trace_remove(self, mode, handle):
        self.fn = None


class TestQuality(unittest.TestCase):
    def test_image_validation(self):
        self.assertFalse(validate_image_path('')[0])
        self.assertFalse(validate_image_path('nonexistent.png')[0])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'a.png'
            p.write_bytes(b'not an image')
            self.assertTrue(validate_image_path(p)[0])
            self.assertFalse(validate_image_path(p, inspect_content=True)[0])

    def test_button(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'a.jpg'; p.write_bytes(b'data')
            var = FakeVar(''); button = MagicMock()
            ctl = AnalyzeButtonController(button, var)
            self.assertEqual(button.configure.call_args.kwargs['state'], 'disabled')
            var.set(str(p))
            self.assertEqual(button.configure.call_args.kwargs['state'], 'normal')
            ctl.set_busy(True)
            self.assertEqual(button.configure.call_args.kwargs['state'], 'disabled')
            ctl.set_busy(False)
            self.assertEqual(button.configure.call_args.kwargs['state'], 'normal')
            ctl.detach()

    def test_blacklist_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'blacklist.json'
            store = BlacklistStore(p, initial_tags=['solo'])
            store.replace_from_text('multiple_views\n# note\nteacher\n')
            self.assertTrue(store.allowed('solo'))
            self.assertEqual(store.filter_tags(['teacher', '1girl']), ['1girl'])
            self.assertEqual(BlacklistStore(p).tags, {'multiple_views', 'teacher'})

    def test_scores(self):
        scores = normalize_scores([{'tag': 'Standing', 'score': .04}, {'tag': 'full_body', 'confidence': .9}])
        self.assertEqual(scores['standing'], .04)
        with tempfile.TemporaryDirectory() as d:
            csv_path, txt_path = export_confidences(scores, 'a.png', d, include_timestamp=False)
            self.assertIn('full_body', csv_path.read_text(encoding='utf-8-sig'))
            self.assertIn('lying', txt_path.read_text(encoding='utf-8'))
            self.assertIn('N/D', txt_path.read_text(encoding='utf-8'))
        with self.assertRaises(ValueError):
            normalize_scores({'bad': float('nan')})

    def test_counts_conflicts(self):
        r = estimate_character_count(['1girl', '2girls', '1boy'])
        self.assertTrue(r.ambiguous)
        self.assertIsNone(r.warning)
        r = estimate_character_count(['2girls', '1boy'], distinct_character_tags=['a', 'b'])
        self.assertEqual(r.minimum, 3)
        self.assertIn('3', r.warning)
        r = estimate_character_count(['1girl', '1girl', 'solo'], distinct_character_tags=['a'])
        self.assertEqual(r.minimum, 1)
        self.assertIsNone(r.warning)
        r = estimate_character_count(['1girl', 'multiple_girls'])
        self.assertTrue(r.ambiguous)
        r = estimate_character_count(['2girls', 'multiple_views'], multiple_views=True)
        self.assertTrue(r.ambiguous)
        self.assertIsNone(r.warning)
        r = estimate_character_count(['1girl', 'solo', 'multiple_views'], multiple_views=True)
        self.assertTrue(r.ambiguous)
        self.assertIsNone(r.warning)


if __name__ == '__main__':
    unittest.main()
