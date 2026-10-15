# Validation record

The release checks and live trial results are being recorded here as they complete.
This file distinguishes deterministic tests from real inference and will retain
failures that informed fixes.

## Verified so far

- 38 deterministic tests passed on Python 3.11 locally and in a Linux container.
- All three baseline evaluators ran in Linux, including C compilation and Node.
- A wheel built successfully.
- Initial live Codex trials improved Python slugging from 3/12 to 12/12 and JavaScript
  CSV parsing from 4/12 to 12/12, then stopped on plateau.
- The initial C trial produced 12/12 candidates, but independent review rejected
  numerical edge cases outside the visible tests. No candidate was promoted.
  This exposed missing review feedback in planning; that feedback is now included,
  and another eligible candidate can be tried after the leading candidate is rejected.
- A live local Ollama `granite4:latest` inference probe returned valid JSON.

A fresh three-project run, local-worker trial, and self-improvement trial are
required before the final validation record is considered complete.
