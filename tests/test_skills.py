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
    def test_role_packet(self):
        self.assertIn('test-driven-development',prelude('worker'))
        self.assertIn('writing-plans',prelude('planner'))
        self.assertIn('requesting-code-review',prelude('reviewer'))
        self.assertLess(len(prelude('worker').encode()),5000)
