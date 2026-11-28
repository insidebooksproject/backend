"""Secrets taken out of free text before it is written to an error log.

The patterns follow the error digest's redaction
(``scripts/error-digest/ibp-error-digest``, a standalone script, so they
are copied rather than imported): a URL's user and password, an
Authorization scheme's credentials, the value of a ``key=``, ``token=``,
``password:`` ... pair (a quoted value whole, spaces and escaped quotes
and all), a JWT, and any long run of base64 or hex with both letters and
digits in it. A structured value whose key names a credential
(:func:`is_credential_key`) is replaced whole, whatever it is.

Only secrets: inmate names, numbers and addresses are kept, as they are
largely public and help find a cause (decided 2026-11-28).

One difference from the digest: a long run may not span a ``/``, so the
segments of a URL path or file path in a stack frame (``/assets/
index-DkS8f3jK.js``, ``python3.13/site-packages``) are never taken for a
token. A base64 secret with a ``/`` in it is still caught by its key
(``key=``, ``Bearer``) where it has one.

Stack frames survive: a pair's separator never crosses a line (``missing
cookie:`` then a frame on the next line keeps the frame's ``at``), and an
unquoted value stops before a ``)`` and before a ``:line:col`` suffix, so
``(http://host/app.js?token=abc:12:34)`` keeps ``:12:34)``.
"""

import re

SECRET = "<secret>"

USERINFO = re.compile(r"(?<=://)[^\s/@]+@")
AUTH_SCHEME = re.compile(r"\b(?P<scheme>Bearer|Basic|Token)\s+[\w.~+/=-]{8,}", re.I)
# The key may be quoted (JSON: ``"password": "..."``); the separator stays
# on one line; the value is a quoted string, whole (escaped quotes too, to
# the end of the line if it isn't closed), or a run up to a separator, a
# parenthesis or a frame's ``:line:col`` suffix. The key is tried only
# at the start of a word (not at each ``-`` inside one, as ``\b`` would),
# so a long ``a-a-a-...`` run costs one try, not one per dash.
SECRET_PAIR = re.compile(
    r"(?P<key>(?<![\w-])[\w-]*(?:key|token|secret|password|passwd|pwd|signature|sig"
    r"|auth|authorization|session|cookie|credentials?)s?)"
    r"(?P<sep>[\"']?[ \t]*[=:][ \t]*)(?!(?:Bearer|Basic|Token)\s)"
    r"(?P<value>\"(?:[^\"\\\n]|\\.)*\"?|'(?:[^'\\\n]|\\.)*'?"
    r"|[^\s&\"',;<>()]+?(?=:\d+:\d+(?:[)\s]|$)|[\s&\"',;<>()]|$))",
    re.I,
)
JWT = re.compile(r"\beyJ[\w-]+\.[\w-]+(?:\.[\w-]+)?", re.A)
TOKEN = re.compile(
    r"(?<![\w+=-])(?=[\w+=-]*\d)(?=[\w+=-]*[A-Za-z])[\w+=-]{24,}(?![\w+=-])",
    re.A,
)


# The words of a key that names a credential (``client_secret``,
# ``x-api-key``, ``sessionId``, ``PASSWORD``); ``api`` then ``key`` is one.
CREDENTIAL_WORDS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "pwd",
        "secret",
        "token",
        "apikey",
        "auth",
        "authorization",
        "cookie",
        "session",
        "credential",
        "signature",
    }
)
KEY_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def is_credential_key(key: str) -> bool:
    """Whether ``key`` names a credential: one of its words (split at
    ``_``, ``-``, ``.``, spaces and camel case; a plural counts) is one of
    ``CREDENTIAL_WORDS``, or it has ``api`` then ``key``."""
    words = [word.lower() for word in KEY_WORD.findall(key)]
    words = [word[:-1] if word.endswith("s") else word for word in words]
    pairs = (a + b for a, b in zip(words, words[1:]))
    return any(word in CREDENTIAL_WORDS for word in words) or "apikey" in pairs


def scrub_secrets(text: str) -> str:
    """``text`` with each secret the patterns find replaced by ``<secret>``."""
    out = USERINFO.sub(f"{SECRET}@", text)
    out = AUTH_SCHEME.sub(lambda m: f"{m['scheme']} {SECRET}", out)
    out = SECRET_PAIR.sub(lambda m: f"{m['key']}{m['sep']}{SECRET}", out)
    out = JWT.sub(SECRET, out)
    return TOKEN.sub(SECRET, out)
