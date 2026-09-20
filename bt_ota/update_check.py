from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import NamedTuple, Optional

REPO = "Aes-App/firmware-updater"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_URL = "https://github.com/%s/releases/latest" % REPO

OPT_OUT_ENV = "AESAPP_NO_UPDATE_CHECK"

_TIMEOUT = 6.0


class Result(NamedTuple):

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
    req = urllib.request.Request(url, headers={
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
    except Exception as e:
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
    if is_newer(current, tag):
        return Result(True, False, current, latest, page,
                      "You are running %s, which is ahead of the newest published "
                      "release (%s)." % (current, latest))
    return Result(True, False, current, latest, page,
                  "You are running the newest release (%s)." % current)


if __name__ == "__main__":
    from bt_ota.gui import VERSION

    r = check(VERSION)
    print(r.message)
    print(r.url)
