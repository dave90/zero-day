"""Shared exact-instance scope fixture for opt-in Gruyere plumbing/product tests."""

GRUYERE_URL = (
    "https://google-gruyere.appspot.com/"
    "554906294150075200827896525189694841833/"
)

PENTEST_MD_TEMPLATE = """\
# Google Gruyere pentest (unauthenticated, e2e)

Target is Google's intentionally-vulnerable training app. It is
authorized for security testing — that is its entire purpose.

## Scope

- in scope:
  - {gruyere_url}
  - any path under that prefix
- out of scope:
  - everything else on appspot.com / google.com
  - any other Gruyere instance ID

## Goal

Find ONE unauthenticated web-security defect. Be quick — don't enumerate
exhaustively. The /snippets.gtl endpoint with a `uid` parameter is a
known-fast win (reflected XSS) but any defect class is acceptable. Once
you have one with concrete evidence, call `finding_submit`.
"""
