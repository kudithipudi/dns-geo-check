"""Unit tests for the hostname sanity check in app.main._validate_hostname.

Resolution happens at Cloudflare, not from this box, so this filter isn't a
security boundary — but it's the only thing keeping obvious junk out of the
DoH query and out of the Worker (whose NAME_RE must stay in step with it).
"""

import pytest

from app.main import _validate_hostname


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("lab.kudithipudi.org", "lab.kudithipudi.org"),
        ("  Lab.Kudithipudi.ORG.  ", "lab.kudithipudi.org"),  # trimmed, de-dotted, lowered
        ("a.b.co", "a.b.co"),  # single-char labels are legal
        ("xn--80ak6aa92e.com", "xn--80ak6aa92e.com"),  # punycode / IDN
        ("my-host.example.com", "my-host.example.com"),  # interior hyphens
    ],
)
def test_accepts_plausible_hostnames(raw, expected):
    assert _validate_hostname(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "localhost",
        "example.local",
        "single-label",
        "8.8.8.8",  # bare IPv4
        "2606:4700:4700::1111",  # bare IPv6
        "-lead.example.com",  # label starts with hyphen
        "trail-.example.com",  # label ends with hyphen
        "under_score.example.com",  # underscores aren't hostname chars (Worker rejects them too)
        "space host.example.com",
        "a..b.com",  # empty label
        f"{'a' * 64}.example.com",  # label over 63 chars
        "a." + "b" * 250 + ".com",  # total over 253 chars
    ],
)
def test_rejects_junk(raw):
    with pytest.raises(ValueError):
        _validate_hostname(raw)
