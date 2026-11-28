"""ibp/scrub.py: secrets taken out of free text before it is logged."""

import time

import pytest

from ibp.scrub import scrub_secrets


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


@pytest.mark.parametrize("text", ["a-" * 8000, "k_" * 8000, "a" * 16000, "1a" * 8000])
def test_a_long_run_is_cheap(text):
    """A 16 KiB body can't make the patterns backtrack for seconds."""
    start = time.perf_counter()
    scrub_secrets(text)
    assert time.perf_counter() - start < 0.5
