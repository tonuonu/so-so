Improve csv.js parseCSV(text). Return arrays of string fields. Support comma separators,
quoted commas, escaped double quotes, CRLF or LF record separators, newlines inside
quoted fields, empty fields and Unicode. Empty input returns []. A trailing record
separator must not create an extra record. Malformed unterminated quotes should throw.
No dependencies. Do not change evaluate.py.
