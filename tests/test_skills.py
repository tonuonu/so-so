import unittest
from soso.skills import catalog, read_skill, prelude

class SkillTests(unittest.TestCase):
    def test_pin_and_pages(self):
        listing=catalog()
        self.assertIn('test-driven-development',listing)
        page=read_skill('test-driven-development',1,20)
        self.assertIn('test-driven-development',page['content'])
        self.assertGreater(page['total_lines'],20)
        self.assertEqual(page['revision'],'bb92a77741419a4ab5f06e711a283343f1ada0c3')
    def test_traversal_rejected(self):
        for name in ('../LICENSE','/etc/passwd','unknown'):
            with self.assertRaises(ValueError): read_skill(name)
    def test_page_bounds_keep_their_reading_but_overflow_is_an_error(self):
        # JSON reads 1e999 as infinity; int() of it raised OverflowError, which the worker loop does not catch.
        for value, start in ((2, 2), (2.0, 2), ('10', 10), (10.7, 10), (True, 1)):  # accepted as before
            self.assertEqual(read_skill('test-driven-development', value, value)['start'], start)
        for value in (float('inf'),float('-inf'),float('nan'),'abc',None,[1]):
            for key in ('start','lines'):
                with self.subTest(key=key,value=value):
                    with self.assertRaises(Exception) as caught:read_skill('test-driven-development',**{key:value})
                    self.assertIsInstance(caught.exception,ValueError)
    def test_role_packet(self):
        self.assertIn('test-driven-development',prelude('worker'))
        self.assertIn('writing-plans',prelude('planner'))
        self.assertIn('requesting-code-review',prelude('reviewer'))
        self.assertLess(len(prelude('worker').encode()),5000)
