"""Is a newer release out than the one running?

WHAT IT DOES. Asks GitHub for the newest published release of
``Aes-App/firmware-updater``, compares its tag with the running ``VERSION``, and
hands back the release page's URL so the app can point the operator at it. It
never downloads, never installs and never touches the filesystem: this app
writes firmware to radios, and a self-updater that could be talked into running
a downloaded binary is a far bigger promise than "there is a 0.9.1".

WHY GITHUB AND NOT AesApp's OWN SERVER. The releases ARE on GitHub -- it is where
the download link on cps.aes.app points -- so asking anywhere else would be
asking a second source about the first one. The call is unauthenticated, sends no identity beyond a
User-Agent, and asks about a public repository.

WHAT IT PROMISES THE CALLER. `check()` returns a Result and NEVER raises: no
network, a rate-limit, a repository with no releases yet and a tag nobody can
parse all come back as ``ok=False`` with something sayable in ``message``. A
version check that can take down the window it is decorating is worse than no
version check, and this one runs on a worker thread where an exception would be
invisible.

Run by hand:  python -m bt_ota.update_check
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import NamedTuple, Optional

#: The public mirror. The GitLab origin holds the full history and is not
#: reachable without an account, so it is the wrong thing to point an operator at.
REPO = "Aes-App/firmware-updater"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
#: Where the operator is sent. A release the API told us about carries its own
#: html_url; this is the fallback, and it resolves to the same page.
RELEASES_URL = "https://github.com/%s/releases/latest" % REPO

#: Set to anything non-empty to stop the automatic check at startup. The button
#: in About still works -- this turns off the one nobody asked for, which is what
#: an air-gapped bench or a CI box wants.
OPT_OUT_ENV = "AESAPP_NO_UPDATE_CHECK"

_TIMEOUT = 6.0


class Result(NamedTuple):
    """The whole answer. ``ok`` False means "we could not find out", which is not
    the same as "you are up to date" and must never be shown as if it were."""

    ok: bool
    newer: bool
    current: str
    latest: str
    url: str
    message: str


def opted_out() -> bool:
    return bool(os.environ.get(OPT_OUT_ENV, "").strip())


_NUM = re.compile(r"\d+")


def parse_version(text: str) -> Optional[tuple]:
    """``'v0.9.1'`` -> ``((0, 9, 1), 1, '')``; ``'0.9.1-beta2'`` -> ``((0, 9, 1), 0, 'beta2')``.

    THREE parts, kept apart on purpose. The numbers are their own tuple so they
    can be zero-padded before comparing -- 0.9 and 0.9.0 are the same version,
    and a flat tuple that mixed the numbers with anything else would make one of
    them "newer" than the other. The middle flag is semver's rule that a
    pre-release sorts BELOW the release it leads to: 0.9.1 must beat 0.9.1-rc1,
    or someone running the candidate is told the final release is not newer.

    Returns None for anything with no numbers in it at all -- a tag like
    ``latest`` is not a version, and guessing at one is worse than saying so.
    """
    tag = (text or "").strip().lstrip("vV")
    if not tag:
        return None
    head = re.split(r"[-+]", tag, maxsplit=1)
    nums = tuple(int(n) for n in _NUM.findall(head[0]))
    if not nums:
        return None
    pre = head[1] if len(head) > 1 and head[1] else ""
    return nums, (0 if pre else 1), pre


def is_newer(latest: str, current: str) -> bool:
    """Is ``latest`` a later version than ``current``?

    Unparseable on either side is False, not True: the failure mode of guessing
    "yes" is nagging every operator on every launch about an update that may not
    exist. Shorter tuples are padded with zeroes so 0.9 and 0.9.0 compare equal
    rather than by length.
    """
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    (an, aflag, apre), (bn, bflag, bpre) = a, b
    n = max(len(an), len(bn))
    an = an + (0,) * (n - len(an))
    bn = bn + (0,) * (n - len(bn))
    return (an, aflag, apre) > (bn, bflag, bpre)


def check(current: str, url: str = API_URL, timeout: float = _TIMEOUT,
          opener=urllib.request.urlopen) -> Result:
    """Ask GitHub, and answer without raising. ``opener`` is for the tests."""
    req = urllib.request.Request(url, headers={
        # GitHub refuses an API request with no User-Agent, and a request that
        # names the app is the polite version of one that does not.
        "User-Agent": "AesApp-Radio-Updater/%s" % current,
        "Accept": "application/vnd.github+json",
    })
    try:
        with opener(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            msg = "No release has been published yet."
        elif e.code in (403, 429):
            # 60 unauthenticated calls an hour, per IP. Worth naming, because an
            # office behind one address can hit it without anyone doing anything
            # wrong, and "check failed" would send someone hunting a bug.
            msg = "GitHub is rate-limiting update checks right now. Try again later."
        else:
            msg = "GitHub answered %s." % e.code
        return Result(False, False, current, "", RELEASES_URL, msg)
    except (urllib.error.URLError, OSError):
        return Result(False, False, current, "", RELEASES_URL,
                      "Could not reach GitHub — check the network, or open the "
                      "releases page yourself.")
    except (ValueError, TypeError):
        return Result(False, False, current, "", RELEASES_URL,
                      "GitHub's answer could not be read.")
    except Exception as e:  # noqa: BLE001 - this module's docstring promises it never raises
        return Result(False, False, current, "", RELEASES_URL,
                      "Update check failed: %s" % e)

    if not isinstance(payload, dict):
        return Result(False, False, current, "", RELEASES_URL,
                      "GitHub's answer could not be read.")
    tag = str(payload.get("tag_name") or payload.get("name") or "").strip()
    page = str(payload.get("html_url") or "").strip() or RELEASES_URL
    if parse_version(tag) is None:
        why = ("GitHub named no release." if not tag else
               "The newest release is named %r, which is not a version this can "
               "compare." % tag)
        return Result(False, False, current, tag, page, why)

    latest = tag.lstrip("vV")
    if is_newer(tag, current):
        return Result(True, True, current, latest, page,
                      "Version %s is available — you are running %s." % (latest, current))
    # Three outcomes, not two. A build that is AHEAD of the newest published
    # release is the normal state on the bench and for anyone testing an
    # unreleased build, and telling them "you are running the newest release"
    # when they are not running a release at all reads as a broken check.
    if is_newer(current, tag):
        return Result(True, False, current, latest, page,
                      "You are running %s, which is ahead of the newest published "
                      "release (%s)." % (current, latest))
    return Result(True, False, current, latest, page,
                  "You are running the newest release (%s)." % current)


if __name__ == "__main__":  # pragma: no cover - a hand check, not a test
    from bt_ota.gui import VERSION

    r = check(VERSION)
    print(r.message)
    print(r.url)
