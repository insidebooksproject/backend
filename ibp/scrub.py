"""Secrets taken out of free text before it is written to an error log.

The patterns follow the error digest's redaction
(``scripts/error-digest/ibp-error-digest``, a standalone script, so they
are copied rather than imported): a URL's user and password, an
Authorization scheme's credentials, the value of a ``key=``, ``token=``,
``password:`` ... pair (a quoted value whole, spaces and all), a JWT, and
any long run of base64 or hex with both letters and digits in it.

Only secrets: inmate names, numbers and addresses are kept, as they are
largely public and help find a cause (decided 2026-11-28).

One difference from the digest: a long run may not span a ``/``, so the
segments of a URL path or file path in a stack frame (``/assets/
index-DkS8f3jK.js``, ``python3.13/site-packages``) are never taken for a
token. A base64 secret with a ``/`` in it is still caught by its key
(``key=``, ``Bearer``) where it has one.
"""

import re

SECRET = "<secret>"

USERINFO = re.compile(r"(?<=://)[^\s/@]+@")
AUTH_SCHEME = re.compile(r"\b(?P<scheme>Bearer|Basic|Token)\s+[\w.~+/=-]{8,}", re.I)
# The key may be quoted (JSON: ``"password": "..."``); the value is a
# quoted string, whole, or a run up to a separator. The key is tried only
# at the start of a word (not at each ``-`` inside one, as ``\b`` would),
# so a long ``a-a-a-...`` run costs one try, not one per dash.
SECRET_PAIR = re.compile(
    r"(?P<key>(?<![\w-])[\w-]*(?:key|token|secret|password|passwd|pwd|signature|sig"
    r"|auth|authorization|session|cookie|credentials?)s?)"
    r"(?P<sep>[\"']?\s*[=:]\s*)(?!(?:Bearer|Basic|Token)\s)"
    r"(?P<value>\"[^\"]*\"?|'[^']*'?|[^\s&\"',;<>]+)",
    re.I,
)
JWT = re.compile(r"\beyJ[\w-]+\.[\w-]+(?:\.[\w-]+)?", re.A)
TOKEN = re.compile(
    r"(?<![\w+=-])(?=[\w+=-]*\d)(?=[\w+=-]*[A-Za-z])[\w+=-]{24,}(?![\w+=-])",
    re.A,
)


def scrub_secrets(text: str) -> str:
    """``text`` with each secret the patterns find replaced by ``<secret>``."""
    out = USERINFO.sub(f"{SECRET}@", text)
    out = AUTH_SCHEME.sub(lambda m: f"{m['scheme']} {SECRET}", out)
    out = SECRET_PAIR.sub(lambda m: f"{m['key']}{m['sep']}{SECRET}", out)
    out = JWT.sub(SECRET, out)
    return TOKEN.sub(SECRET, out)
