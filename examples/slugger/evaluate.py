import json
from slugger import slug
cases = [('', ''), ('hello', 'hello'), ('Hello World', 'hello-world'),
         ('  Hello  World  ', 'hello-world'), ('a___b', 'a-b'),
         ('Crème brûlée', 'creme-brulee'), ('A/B+C', 'a-b-c'),
         ('one---two', 'one-two'), ('123 test!', '123-test'),
         ('中文', ''), ('foo\tbar\nbaz', 'foo-bar-baz'), ('über Straße', 'uber-stra-e')]
passed = sum(slug(value) == expected for value, expected in cases)
print(json.dumps({'score': passed, 'total': len(cases)}))
