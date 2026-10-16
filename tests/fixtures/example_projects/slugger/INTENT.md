Improve slugger.py. slug(text) must normalize Unicode with NFKD, remove combining marks,
keep ASCII letters and digits only, replace each run of other characters with one dash,
strip leading/trailing dashes and lowercase. Empty input returns empty string.
Use the standard library. Improve evaluator accuracy without changing evaluate.py.
