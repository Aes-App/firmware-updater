"""The server side of a codeplug write: claim the launch token, download the
prepared segments, keep the project's lock alive, report the outcome.

Same transport policy as radio_contacts.catalog (stdlib urllib + certifi, the
same retry/backoff), with three differences that come from what this flow IS:

  * the token carries a JOB, not a catalogue. The server tells us which project
    and which prepared build we are writing; the link itself carries nothing but
    random bytes, so a doctored URL cannot retarget the write.
  * the project is LOCKED while we work, and the lock is a lease: every
    heartbeat renews it. That is what makes a crash safe — if this app dies, the
    lease runs out on its own and the owner gets their project back. It also
    means a heartbeat must not spend 30 seconds backing off, so heartbeats do
    not retry.
  * losing the lease is fatal to the write, not a transport hiccup: the server
    answers 409 and we stop rather than keep writing a codeplug the server has
    already told someone else it is finished with.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urljoin

from radio_contacts import catalog as _cat
from radio_contacts import segments as seg

SESSION_PATH = "/api/desktop/write/session"
SEGMENTS_PATH = "/api/desktop/write/segments"
HEARTBEAT_PATH = "/api/desktop/write/heartbeat"
OUTCOME_PATH = "/api/desktop/write/outcome"

# Statuses this flow reads rather than treats as transport failures.
_PASS = (400, 401, 403, 404, 409, 410)

OUTCOME_SUCCESS = "success"
OUTCOME_VERIFY_FAILED = "verify_failed"
OUTCOME_WRITE_FAILED = "write_failed"
OUTCOME_VERIFY_UNAVAILABLE = "verify_unavailable"


class CodeplugClientError(_cat.ContactsError):
    """A server-side refusal or a malformed reply. Operator-facing message."""


class LockLostError(CodeplugClientError):
    """The project's write lease expired or was cancelled — the write must stop.

    Not a retry: the server has told the owner their project is free again, and
    a codeplug written after that could disagree with what they now see.
    """


@dataclass
class CodeplugJob:
    """What the server says this link is for."""
    job_id: str
    project_id: int
    project_uuid: str
    project_name: str
    model: str
    lease_seconds: int
    # What the radio must answer with in PC mode. The server holds the identity
    # table; an empty list means it did not say, and the app then does not
    # enforce a model rather than refusing every radio.
    ident_tokens: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    bandplan_mode: Optional[int] = None


class CodeplugSession:
    """One desktop write, from claiming the link to reporting the outcome."""

    def __init__(self, base_url: str, token: str, timeout: float = 20.0,
                 on_status: Optional[Callable[[str], None]] = None):
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token
        self.timeout = timeout
        self.on_status = on_status
        self.job: Optional[CodeplugJob] = None

    # ── the four calls ──────────────────────────────────────────────────────
    def claim(self) -> CodeplugJob:
        """POST …/session: claim the link and learn the job."""
        d = self._call("POST", SESSION_PATH)
        if not d.get("ok"):
            raise CodeplugClientError("The server did not open a write session for this link. "
                                      "Start the write again in the web app.")
        p = d.get("project") or {}
        self.job = CodeplugJob(
            job_id=str(d.get("jobId") or ""),
            project_id=int(p.get("id") or 0),
            project_uuid=str(p.get("uuid") or ""),
            project_name=str(p.get("name") or "(unnamed)"),
            model=str(p.get("model") or ""),
            lease_seconds=int(d.get("leaseSeconds") or 600),
            ident_tokens=[t for t in (d.get("identTokens") or []) if isinstance(t, str)],
            stats=d.get("stats") if isinstance(d.get("stats"), dict) else {},
            bandplan_mode=d.get("bandplanMode") if isinstance(d.get("bandplanMode"), int) else None,
        )
        if not self.job.job_id or not self.job.model:
            raise CodeplugClientError("The server's write session was incomplete. Try the web app again.")
        return self.job

    def fetch_plan(self, on_progress: Optional[Callable[[int, int], None]] = None) -> "WritePlan":
        """GET …/segments: the prepared envelope, decoded into a write plan."""
        d = self._call("GET", SEGMENTS_PATH, on_progress=on_progress)
        return WritePlan.from_envelope(d)

    def heartbeat(self, phase: str, done: int = 0, total: int = 0, message: str = "") -> bool:
        """Renew the lease and report progress. False when the network dropped
        it (we keep writing — a missed beat is not a lost lease); LockLostError
        when the SERVER says the lease is gone, which does stop the write."""
        try:
            self._call("POST", HEARTBEAT_PATH, payload={
                "phase": phase[:32], "done": int(done), "total": int(total),
                "message": message[:255],
            }, retry=False)
            return True
        except LockLostError:
            raise
        except _cat.ContactsError:
            return False

    def report(self, outcome: str, detail: Optional[dict] = None, message: str = "") -> bool:
        """POST …/outcome: how the write ended, and give the lease back. Never
        raises — a write that reached the radio must not be reported as failed
        because the report itself could not be delivered."""
        try:
            self._call("POST", OUTCOME_PATH, payload={
                "outcome": outcome, "detail": detail or None, "message": message[:255],
            })
            return True
        except _cat.ContactsError:
            return False

    # ── transport ───────────────────────────────────────────────────────────
    def _call(self, method: str, path: str, payload: Optional[dict] = None,
              retry: bool = True,
              on_progress: Optional[Callable[[int, int], None]] = None) -> dict:
        url = urljoin(self.base_url, path.lstrip("/"))
        status, body = _cat._request(
            method, url, self.token, self.timeout,
            on_progress=on_progress, on_status=self.on_status,
            payload=payload, retry=retry, pass_through=_PASS,
        )
        if status in _PASS:
            raise self._refusal(status, body)
        try:
            d = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise CodeplugClientError("The server's reply was not readable. Try again in a moment.")
        if not isinstance(d, dict):
            raise CodeplugClientError("The server returned an unexpected reply.")
        return d

    @staticmethod
    def _refusal(status: int, body: bytes) -> CodeplugClientError:
        code, message = f"http_{status}", ""
        try:
            d = json.loads(body.decode("utf-8"))
            if isinstance(d, dict):
                code = str(d.get("error") or code)
                message = str(d.get("message") or "")
        except (ValueError, UnicodeDecodeError):
            pass
        if code in ("lock_lost", "not_holder"):
            return LockLostError(message or "The web app took this write back. Nothing more was written.")
        friendly = {
            "invalid_token": "This link is not valid. Start the write again in the web app.",
            "token_expired": "This link has expired. Start the write again in the web app.",
            "token_superseded": "A newer link replaced this one. Use the most recent link.",
            "invalid_session": "The write session has ended. Start the write again in the web app.",
            "wrong_purpose": "That link is not a codeplug write.",
            "job_gone": "This write is no longer available on the server.",
            "segments_gone": "The prepared write data has expired. Prepare it again in the web app.",
        }.get(code)
        return CodeplugClientError(friendly or message or f"The server refused the request (HTTP {status}).")


@dataclass(frozen=True)
class WritePlan:
    """The prepared write, split the way the radio needs it written.

    The codeplug and the (optional, much larger) digital contact database are
    two SEPARATE commits: a session that holds the codeplug uncommitted while a
    multi-megabyte contact list streams has been observed to corrupt stray
    codeplug bytes, so contacts are their own phase after the codeplug's END —
    exactly as the browser driver and the factory CPS do it.

    WHERE the contacts live is the radio's business, not ours: the address map
    differs between the 878 family and the D890, and a wrong guess would write
    contact pages inside the codeplug phase — the very corruption the split
    exists to avoid. So the split comes from the server's `contactRegions`, and
    a plan that claims to carry contacts without saying where they are is
    REFUSED rather than guessed at.
    """
    codeplug: list
    contacts: list
    stats: dict

    @property
    def blocks(self) -> int:
        return seg.block_count(self.codeplug) + seg.block_count(self.contacts)

    @staticmethod
    def from_envelope(env: dict) -> "WritePlan":
        """Decode {"segments": [{"addr": "02500000", "hex": "…"}, …]}."""
        raw = env.get("segments")
        if not isinstance(raw, list) or not raw:
            raise CodeplugClientError("The prepared write data was empty. Prepare it again in the web app.")
        regions = _contact_regions(env)
        code, cont = [], []
        for i, s in enumerate(raw):
            if not isinstance(s, dict):
                raise CodeplugClientError(f"Write segment {i} is malformed.")
            addr_s, hex_s = s.get("addr"), s.get("hex")
            if not isinstance(addr_s, str) or not isinstance(hex_s, str):
                raise CodeplugClientError(f"Write segment {i} is malformed.")
            try:
                addr = int(addr_s, 16)
                data = bytes.fromhex(hex_s)
            except ValueError:
                raise CodeplugClientError(f"Write segment {i} is not readable hex.")
            if not data or len(data) % seg.BLOCK != 0:
                raise CodeplugClientError(
                    f"Write segment {i} @0x{addr:08x} is {len(data)} bytes — not whole 16-byte blocks. "
                    "A short download must never become a short write.")
            is_contact = any(lo <= addr < hi for lo, hi in regions)
            (cont if is_contact else code).append(seg.Segment(addr, data))
        code.sort(key=lambda s: s.addr)
        cont.sort(key=lambda s: s.addr)
        if env.get("contactsIncluded") and not cont:
            raise CodeplugClientError(
                "The server says this write includes the digital contact list but none of the "
                "prepared data falls in the contact region. Refusing to write rather than send "
                "contact pages in the codeplug phase. Prepare the write again in the web app.")
        stats = env.get("stats") if isinstance(env.get("stats"), dict) else {}
        return WritePlan(codeplug=code, contacts=cont, stats=stats)


def _contact_regions(env: dict) -> list:
    """[[lo, hi), …] from the envelope. Absent is fine ONLY when the write
    carries no contacts at all — then everything is codeplug and there is
    nothing to misfile."""
    raw = env.get("contactRegions")
    if raw is None:
        if env.get("contactsIncluded"):
            raise CodeplugClientError(
                "This server did not say where the digital contact list lives, so the write "
                "cannot be split into its two phases. Update the web app, or write from the "
                "browser instead.")
        return []
    if not isinstance(raw, list):
        raise CodeplugClientError("The server's contact-region list was not readable.")
    out = []
    for r in raw:
        if (not isinstance(r, (list, tuple)) or len(r) != 2
                or not all(isinstance(v, int) for v in r) or r[0] >= r[1]):
            raise CodeplugClientError("The server's contact-region list was not readable.")
        out.append((int(r[0]), int(r[1])))
    return out
