import json
import unittest

from soso.models import object_json


class ModelProtocolTests(unittest.TestCase):
    def test_observed_qwen_tool_wrapper(self):
        reply = '<tool_call>\n{"tool":"read","path":"game.js","start":81,"lines":144}\n</invoke>\n</parameter>\n</function>\n</tool_call>'
        self.assertEqual(object_json(reply), {'tool': 'read', 'path': 'game.js', 'start': 81, 'lines': 144})

    def test_wrapper_preserves_literal_tags_and_braces_inside_source(self):
        action = {'tool': 'write', 'path': 'index.html', 'content': '<p>{hello}</tool_call></p>'}
        self.assertEqual(object_json('<tool_call>' + json.dumps(action) + '</tool_call>'), action)

    def test_wrapper_rejects_extra_requests_prose_and_incomplete_frames(self):
        for suffix in ('', '</tool_call> extra', '{} </tool_call>', '<invoke></invoke></tool_call>',
                       '</unknown></tool_call>', '</tool_call></tool_call>'):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                object_json('<tool_call>{"tool":"list"}' + suffix)
        for value in ('[]', 'null', '42', '{"tool":'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                object_json('<tool_call>' + value + '</tool_call>')


if __name__ == '__main__':
    unittest.main()
