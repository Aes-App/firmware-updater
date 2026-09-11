"""The version check: comparison, and every way GitHub can fail to answer.

Two things are worth pinning here and nothing else is.

The COMPARISON, because it is the whole feature and it is easy to get subtly
wrong in a way nobody notices: it once reported 0.9 as newer than 0.9.0 (equal
versions written differently), which would have nagged every operator on every
launch about an update that does not exist. Padding, pre-releases and a tag that
is not a version at all are all pinned below.

The PROMISE THAT check() NEVER RAISES, because it runs on a worker thread behind
a window. An exception there is invisible -- no traceback anyone sees, just a
banner that never appears or a button stuck on "Checking GitHub…". Every failure
has to come back as a Result with ok=False and something sayable in it, and
ok=False must never be dressed up as "you are up to date".

No test here touches the network: check() takes its opener, and tests/conftest.py
turns the startup check off for the whole suite.

Run:  python -m pytest tests/test_update_check.py -q
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bt_ota import update_check as uc  # noqa: E402


# -- the comparison ----------------------------------------------------------
@pytest.mark.parametrize("latest,current,expected", [
    ("v0.9.1", "0.9.0", True),          # the ordinary case, tag carries a v
    ("0.9.1", "0.9.0", True),
    ("1.0.0", "0.9.9", True),           # not a string compare: 1 > 0 beats 9 > 0
    ("0.10.0", "0.9.0", True),          # nor a decimal one: 10 > 9
    ("0.9.0", "0.9.0", False),
    ("0.9.0", "0.9.1", False),          # older is not newer
    ("0.9", "0.9.0", False),            # THE regression: padded, these are equal
    ("0.9.0", "0.9", False),
    ("0.9.1-rc1", "0.9.1", False),      # a candidate is below its release
    ("0.9.1", "0.9.1-rc1", True),       # and the release is above the candidate
    ("0.9.1-rc2", "0.9.1-rc1", True),
    ("0.9.1+build7", "0.9.1", False),   # build metadata is not a version bump
    ("latest", "0.9.0", False),         # not a version: never claim newer
    ("", "0.9.0", False),
    ("0.9.1", "", False),               # and unknown on OUR side is not newer either
])
def test_is_newer(latest, current, expected):
    assert uc.is_newer(latest, current) is expected


def test_parse_version_keeps_the_three_parts_apart():
    assert uc.parse_version("v1.2.3") == ((1, 2, 3), 1, "")
    assert uc.parse_version("1.2.3-beta2") == ((1, 2, 3), 0, "beta2")
    assert uc.parse_version("nightly") is None


def test_opted_out_reads_the_environment(monkeypatch):
    monkeypatch.delenv(uc.OPT_OUT_ENV, raising=False)
    assert uc.opted_out() is False
    monkeypatch.setenv(uc.OPT_OUT_ENV, "1")
    assert uc.opted_out() is True
    monkeypatch.setenv(uc.OPT_OUT_ENV, "   ")     # whitespace is not a choice
    assert uc.opted_out() is False


# -- a fake GitHub -----------------------------------------------------------
class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def _opener(payload=None, raises=None, seen=None):
    """An urlopen stand-in. `seen` collects the Request, for the header check."""

    def open_(req, timeout=None):
        if seen is not None:
            seen.append(req)
        if raises is not None:
            raise raises
        body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        return _Resp(body.encode("utf-8") if isinstance(body, str) else body)

    return open_


def _release(tag="v1.0.0", url=None):
    return {"tag_name": tag, "name": tag,
            "html_url": url or "https://github.com/Aes-App/firmware-updater/releases/tag/" + tag}


def test_check_reports_a_newer_release_and_where_to_get_it():
    r = uc.check("0.9.0", opener=_opener(_release("v1.0.0")))
    assert (r.ok, r.newer, r.latest) == (True, True, "1.0.0")
    assert r.url.endswith("/releases/tag/v1.0.0")
    assert "1.0.0" in r.message and "0.9.0" in r.message


def test_check_reports_up_to_date():
    r = uc.check("1.0.0", opener=_opener(_release("v1.0.0")))
    assert (r.ok, r.newer) == (True, False)
    assert "newest release" in r.message


def test_check_says_so_when_the_build_is_ahead_of_the_release():
    # The normal state on the bench and for anyone testing an unreleased build.
    # "You are running the newest release" would read as a check that is lying.
    r = uc.check("1.1.0", opener=_opener(_release("v1.0.0")))
    assert (r.ok, r.newer) == (True, False)
    assert "ahead" in r.message and "1.0.0" in r.message


def test_check_sends_a_user_agent_because_github_refuses_without_one():
    seen = []
    uc.check("0.9.0", opener=_opener(_release(), seen=seen))
    assert seen[0].get_header("User-agent", "").startswith("AesApp-Radio-Updater/")


@pytest.mark.parametrize("err,fragment", [
    (urllib.error.HTTPError("u", 404, "Not Found", {}, None), "No release"),
    (urllib.error.HTTPError("u", 403, "Forbidden", {}, None), "rate-limit"),
    (urllib.error.HTTPError("u", 429, "Too Many", {}, None), "rate-limit"),
    (urllib.error.HTTPError("u", 500, "Server Error", {}, None), "500"),
    (urllib.error.URLError("offline"), "Could not reach"),
    (OSError("socket died"), "Could not reach"),
    (RuntimeError("something nobody predicted"), "failed"),
])
def test_every_failure_comes_back_as_a_result(err, fragment):
    r = uc.check("0.9.0", opener=_opener(raises=err))
    assert r.ok is False, "a failure must never come back as ok"
    assert r.newer is False, "and must never suggest an update it did not find"
    assert fragment.lower() in r.message.lower()
    # There is still somewhere to send the operator: the releases page.
    assert r.url == uc.RELEASES_URL


def test_unreadable_answers_are_failures_not_updates():
    for payload in [b"<html>not json</html>", b"[]", b'{"tag_name": "nightly"}']:
        r = uc.check("0.9.0", opener=_opener(payload))
        assert (r.ok, r.newer) == (False, False), payload
        assert r.message


def test_a_release_with_no_html_url_still_points_at_the_releases_page():
    r = uc.check("0.9.0", opener=_opener({"tag_name": "v2.0.0"}))
    assert r.newer is True
    assert r.url == uc.RELEASES_URL


def test_the_urls_name_the_public_mirror():
    # The GitLab origin needs an account; sending an operator there is a dead end.
    assert uc.API_URL == "https://api.github.com/repos/Aes-App/firmware-updater/releases/latest"
    assert uc.RELEASES_URL == "https://github.com/Aes-App/firmware-updater/releases/latest"
