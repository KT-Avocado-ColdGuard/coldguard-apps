import unittest
from ci.tags import entries

class TagTests(unittest.TestCase):
    def test_hash_tags_are_not_sorted_as_time(self):
        self.assertEqual(len(entries('collector', ['ba2937', '2026-11-23-test1'])), 2)
    def test_manual_filter(self):
        self.assertEqual(entries('controller', ['ba2937', 'abc'], 'ba2937'), [{'service': 'controller', 'tag': 'ba2937'}])
    def test_missing_and_invalid_tags_fail(self):
        for tags, selected in [([], ''), (['abc'], 'missing'), (['bad/tag'], '')]:
            with self.assertRaises(ValueError): entries('simulator', tags, selected)
