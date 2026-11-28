"""Secrets taken out of free text before it is written to an error log.

The patterns follow the error digest's redaction
(``scripts/error-digest/ibp-error-digest``, a standalone script, so they
are copied rather than imported): a URL's user and password, a ping or
webhook URL's path, an Authorization scheme's credentials, the value of a
``key=``, ``token=``, ``password:`` ... pair (a quoted value whole, spaces and escaped quotes
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

Text is scrubbed a line at a time (CRLF, CR, LF, U+2028 and U+2029 all end
one), so a key at the end of one line (``missing cookie:``) never takes the
next one (a stack frame's ``at ...``) as its value, and a quoted value
never runs into the next line. Every line is scrubbed the same way, stack frames too: a value is
replaced whole, digits after it included (``token=example:123:456`` is
``token=<secret>``, in a frame's URL as anywhere).
"""

import re

SECRET = "<secret>"

USERINFO = re.compile(r"(?<=://)[^\s/@]+@")
# Nothing between a scheme or key and its value crosses a line: a key at the
# end of one line must not take the next line (a stack frame) as its value.
AUTH_SCHEME = re.compile(r"\b(?P<scheme>Bearer|Basic|Token)[ \t]+[\w.~+/=-]{8,}", re.I)
# As the digest's: a URL path that is itself the credential: a Healthchecks
# check's ping URL (``hc-ping.com/<uuid>``, ``.../ping/<uuid>`` or
# ``.../ping/<key>/<slug>``, any host) or a webhook's (``.../hooks/<token>``,
# ``.../webhook/...``, ``hooks.slack.com/services/...``). Its secret part
# is a segment of 16 or more [A-Za-z0-9_-]; the rest of the path (a slug,
# ``/start``) goes with it, the ``:line:col`` of a stack frame stays.
CREDENTIAL_PATH = re.compile(
    r"(?P<prefix>(?<![\w.-])(?:hc-ping\.com|ping|hooks|webhooks?)/)"
    r"[\w-]{16,}(?:/[^\s?#\"'<>:]*)?"
    r"|(?P<slack>(?<![\w.-])hooks\.slack\.com/services/)[^\s?#\"'<>:]+",
    re.I | re.A,
)
# As the digest's: the key may be quoted (JSON: ``"password": "..."``, or
# JSON in a JSON string: ``\"password\": \"...\"``); the separator stays on
# one line; the value is a quoted string, whole (escaped quotes too, to the
# end of the line if it isn't closed; atomic, so it can't backtrack), or a
# run up to a separator. The key is tried
# only at the start of a word (not at each ``-`` inside one, as ``\b``
# would), so a long ``a-a-a-...`` run costs one try, not one per dash.
SECRET_PAIR = re.compile(
    r"(?P<key>(?<![\w-])[\w-]*(?:key|token|secret|password|passwd|pwd|signature|sig"
    r"|auth|authorization|session|cookie|credentials?)s?)"
    r"(?P<sep>(?:\\?[\"'])?[ \t]*[=:][ \t]*)(?!(?:Bearer|Basic|Token)[ \t])"
    r"(?P<value>"
    # \"...\" (JSON in a JSON string): \\\" is an escaped quote inside it.
    r"\\\"(?>\\\\\\\"|\\\\\\\\|\\[^\"\\\n]|[^\"\\\n])*(?:\\\"|$)"
    r"|\"(?>\\[^\n]|[^\"\\\n])*\"?"
    r"|'(?>\\[^\n]|[^'\\\n])*'?"
    r"|[^\s&\"',;<>]+)",
    re.I | re.M,
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


# Line separators: CRLF, CR, LF and Unicode's line and paragraph
# separators. The patterns are applied to each line on its own, so none
# (a quoted value in particular) can run from one line into the next.
LINE_BREAK = re.compile("(\r\n|[\r\n\u2028\u2029])")


def scrub_secrets(text: str) -> str:
    """``text`` with each secret the patterns find replaced by ``<secret>``,
    a line at a time (the line separators kept as they were)."""
    parts = LINE_BREAK.split(text)
    # Even indexes are lines, odd ones the separators between them.
    return "".join(
        part if index % 2 else _scrub_line(part) for index, part in enumerate(parts)
    )


def _scrub_line(text: str) -> str:
    out = USERINFO.sub(f"{SECRET}@", text)
    out = CREDENTIAL_PATH.sub(lambda m: f"{m['prefix'] or m['slack']}{SECRET}", out)
    out = AUTH_SCHEME.sub(lambda m: f"{m['scheme']} {SECRET}", out)
    out = SECRET_PAIR.sub(lambda m: f"{m['key']}{m['sep']}{SECRET}", out)
    out = JWT.sub(SECRET, out)
    return TOKEN.sub(SECRET, out)
