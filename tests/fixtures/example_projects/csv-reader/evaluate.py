import json
import subprocess
cases = [('', []), ('a,b', [['a', 'b']]), ('a,b\n', [['a', 'b']]),
         ('a,b\r\nc,d\r\n', [['a', 'b'], ['c', 'd']]),
         ('"a,b",c', [['a,b', 'c']]), ('"a""b",c', [['a"b', 'c']]),
         ('"a\nb",c', [['a\nb', 'c']]), (',,', [['', '', '']]),
         ('"",x', [['', 'x']]), ('é,世界', [['é', '世界']]),
         ('a\n\nb', [['a'], [''], ['b']]), ('"unclosed', None)]
script = '''const {parseCSV}=require('./csv.js');
let input=''; process.stdin.on('data',x=>input+=x); process.stdin.on('end',()=>{
console.log(JSON.stringify(JSON.parse(input).map(([text])=>{
try{return parseCSV(text)}catch(e){return null}}))) });'''
result = subprocess.run(['node', '-e', script], input=json.dumps(cases), text=True,
                        capture_output=True, timeout=10, check=True)
actual = json.loads(result.stdout)
print(json.dumps({'score': sum(a == b for a, (_, b) in zip(actual, cases)), 'total': len(cases)}))
