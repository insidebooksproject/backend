"""ibp/scrub.py: secrets taken out of free text before it is logged."""

import time

import pytest

from ibp.scrub import is_credential_key, scrub_secrets


@pytest.mark.parametrize(
    "text, expected",
    [
        ('password: "correct horse battery" ok', "password: <secret> ok"),
        ("password='correct horse battery'", "password=<secret>"),
        ('{"password": "correct horse", "n": 1}', '{"password": <secret>, "n": 1}'),
        ("api_key=abc&x=1", "api_key=<secret>&x=1"),
        ("X-Api-Key: abc123", "X-Api-Key: <secret>"),
        ("Authorization: Bearer abcdefgh12345678", "Authorization: Bearer <secret>"),
        ("Basic dXNlcjpwYXNzd29yZA==", "Basic <secret>"),
        ("https://user:pw@host/x", "https://<secret>@host/x"),
        ("eyJhbGciOi.eyJzdWIi.c2ln", "<secret>"),
        ("sk-ant-api03-AbCdEf1234567890AbCdEf1234567890", "<secret>"),
    ],
)
def test_secrets_are_replaced(text, expected):
    """Each kind of secret the digest knows, and a quoted multi-word value."""
    assert scrub_secrets(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "SMITH, JOHN 01234567 TEX-00012345-0 at 2001 Main St, Huntsville",
        "at r (http://ibp-server/assets/index-DkS8f3jK.js:1:2345)",
        'File "/app/.venv/lib/python3.13/site-packages/starlette/routing.py", '
        "line 12",
        "KeyError: 'name'",
        "ISBN 9780140449136 not found",
    ],
)
def test_inmate_details_and_frames_are_kept(text):
    """Names, numbers, addresses, codes and stack frames pass unchanged."""
    assert scrub_secrets(text) == text


@pytest.mark.parametrize(
    "text",
    ["a-" * 8000, "k_" * 8000, "a" * 16000, "1a" * 8000, "token=" + "a:1" * 5000],
)
def test_a_long_run_is_cheap(text):
    """A 16 KiB body can't make the patterns backtrack for seconds."""
    start = time.perf_counter()
    scrub_secrets(text)
    assert time.perf_counter() - start < 0.5


def test_a_quoted_value_with_escaped_quotes_is_replaced_whole():
    """Escaped quotes inside a quoted value don't end it early."""
    assert scrub_secrets('password: "a\\"b c" then') == "password: <secret> then"
    assert scrub_secrets(r"secret='it\'s mine' x") == "secret=<secret> x"
    assert (
        scrub_secrets('{"token": "x\\\\" , "n": 1}') == '{"token": <secret> , "n": 1}'
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        # The separator doesn't cross a line: the next frame keeps its "at".
        (
            "TypeError: missing cookie:\n"
            "    at renderRows (http://ibp-server/assets/index-DkS8f3jK.js:10:5)",
            "TypeError: missing cookie:\n"
            "    at renderRows (http://ibp-server/assets/index-DkS8f3jK.js:10:5)",
        ),
        (
            "missing password=\n    at f (a.js:1:2)",
            "missing password=\n    at f (a.js:1:2)",
        ),
        # An unclosed quote ends with its line.
        (
            'password: "abc\n    at f (a.js:1:2)',
            "password: <secret>\n    at f (a.js:1:2)",
        ),
    ],
)
def test_stack_frames_survive(text, expected):
    """A frame on the line after a credential word keeps its "at"."""
    assert scrub_secrets(text) == expected


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "PASSWORD",
        "passwd",
        "passphrase",
        "pwd",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "refreshToken",
        "api_key",
        "apiKey",
        "apikey",
        "api-key",
        "x-api-key",
        "X-API-Key",
        "auth",
        "authorization",
        "cookie",
        "cookies",
        "session",
        "sessionId",
        "credential",
        "credentials",
        "signature",
    ],
)
def test_credential_keys(key):
    """Each credential name, prefixed, camel-cased or plural."""
    assert is_credential_key(key)


@pytest.mark.parametrize(
    "key", ["url", "status", "detail", "package", "operation", "author", "key", "upc"]
)
def test_other_keys(key):
    """Keys that don't name a credential, ``key`` alone among them."""
    assert not is_credential_key(key)


@pytest.mark.parametrize(
    "text, expected",
    [
        # Healthchecks ping URLs (synthetic ids), with what follows the id.
        (
            "GET https://hc-ping.com/0b1c2d3e-aaaa-4bbb-8ccc-1234567890ab/start failed",
            "GET https://hc-ping.com/<secret> failed",
        ),
        (
            "curl https://hc.example.org/ping/abcdefghijklmnop0123/backup-job",
            "curl https://hc.example.org/ping/<secret>",
        ),
        (
            "POST https://hooks.slack.com/services/T000/B000/XXXXXXXX failed",
            "POST https://hooks.slack.com/services/<secret> failed",
        ),
        # A frame's :line:col stays.
        (
            "at f (http://ibp-server/webhook/abcdefghijklmnopqrst:3:4)",
            "at f (http://ibp-server/webhook/<secret>:3:4)",
        ),
        # A short path segment isn't a credential.
        ("GET /ping/ok", "GET /ping/ok"),
    ],
)
def test_credential_paths_are_replaced(text, expected):
    """A ping or webhook URL's path is the credential."""
    assert scrub_secrets(text) == expected


def test_json_in_a_json_string_is_replaced_whole():
    """\\"password\\": \\"...\\" with an escaped quote inside it."""
    text = r"{\"password\": \"a\\\"b c\", \"n\": 1}"
    assert scrub_secrets(text) == r"{\"password\": <secret>, \"n\": 1}"


def test_a_scheme_does_not_take_the_next_line():
    """Bearer at the end of a line keeps the line after it."""
    text = "expected Bearer\n    at checkAuthorization (a.js:1:2)"
    assert scrub_secrets(text) == text


@pytest.mark.parametrize(
    "text, expected",
    [
        # Digits after a value are part of it, in a frame as anywhere.
        ("refused token=example:123:456", "refused token=<secret>"),
        ("token=example:123:456\nnext", "token=<secret>\nnext"),
        ("    at token=example:123:456", "    at token=<secret>"),
        ("f@token=example:1:2", "f@token=<secret>"),
        (
            "    at r (https://ibp-server/a.js?token=example:123:456)",
            "    at r (https://ibp-server/a.js?token=<secret>",
        ),
        (
            "r@moz-extension://abc/content.js?api_key=zz:3:4",
            "r@moz-extension://abc/content.js?api_key=<secret>",
        ),
        # A frame without a secret is left alone, position and all.
        (
            "Error: x\r\n    at async Foo.bar (/app/a.js:1:2)\r\n",
            "Error: x\r\n    at async Foo.bar (/app/a.js:1:2)\r\n",
        ),
    ],
)
def test_a_value_is_replaced_with_its_digits(text, expected):
    """No ``:line:col`` is split off a credential value, in a frame or not."""
    assert scrub_secrets(text) == expected


@pytest.mark.parametrize("separator", ["\r", "\r\n", "\n", " ", " "])
def test_each_line_is_scrubbed_on_its_own(separator):
    """An unclosed quoted value ends with its line, whatever ends it, and
    the separators are kept as they were."""
    text = f'password: \\"letmein{separator}diagnostic "new details"'
    assert scrub_secrets(text) == (
        f'password: <secret>{separator}diagnostic "new details"'
    )
    text = f"missing cookie:{separator}    at renderRows (http://ibp-server/a.js:1:2)"
    assert scrub_secrets(text) == text
    text = f"token=abc{separator}token=def{separator}"
    assert scrub_secrets(text) == f"token=<secret>{separator}token=<secret>{separator}"
