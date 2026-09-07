"""Write a prepared codeplug to the radio, then read it back to prove it.

The wire protocol is the one radio_contacts.engine already speaks (PROGRAM →
identity → W-frames → END); what a CODEPLUG write adds is the shape of the
session, and every part of that shape is load-bearing:

  phase 1  the codeplug — every non-contact block — written and committed with
           its own END. It is committed FIRST because a session that holds the
           codeplug uncommitted while a multi-megabyte contact list streams has
           been observed to corrupt stray codeplug bytes (a D890 write with a
           ~24 MB list dropped two zone-name bytes). The factory CPS splits it
           the same way.
  phase 2  the digital contact database, when the operator asked for one: its
           own PROGRAM…END session after the reboot phase 1's commit causes.
  phase 3  read the codeplug back and compare. A verify failure is NOT a failed
           write — the bytes are committed either way — so it is reported as
           its own outcome, and a radio that will not reopen after a commit is
           reported as "unverifiable" rather than as a failure.

Every commit reboots the radio and drops the USB device, so each phase begins
by waiting for the port to come back. The radio may return under a DIFFERENT
device name (macOS renames cu.usbmodem*), which is why reopening falls back to
scanning for a port that was not there before.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from radio_fw.engines import AbortedError, FirmwareUpdateError, SerialLink
from radio_contacts import engine as ce
from radio_contacts import segments as seg

ACK = ce.ACK
BAUD = ce.BAUD
READ_LL = 16
#: A read REQUEST is 0x52; the radio's reply is headed 0x57 (see _read_block).
READ_REPLY_OPCODE = 0x57
READ_TIMEOUT_MS = 1500
READ_ATTEMPTS = 5
REBOOT_WAIT_S = 30           # the browser driver waits the same before reopening
#: A freshly opened port can still receive the radio's boot banner; settle before
#: flushing, or the flush clears an empty buffer and the banner arrives after it.
OPEN_SETTLE_S = 1.5
RETRY_SETTLE_S = 3.0
REOPEN_TIMEOUT_S = 60
REOPEN_POLL_S = 1.0
PROGRESS_EVERY_BLOCKS = 256
HEARTBEAT_EVERY_S = 20.0
VERIFY_REPORT_LIMIT = 8      # mismatches carried in the outcome report


class CodeplugWriteError(ce.ContactWriteError):
    """A protocol failure during a codeplug session. Operator-facing message."""


@dataclass
class WriteResult:
    """What happened, in the terms the server's outcome endpoint speaks."""
    outcome: str
    blocks_written: int = 0
    contact_blocks: int = 0
    seconds: float = 0.0
    retries: int = 0
    checked: int = 0
    mismatches: int = 0
    first_mismatches: list = field(default_factory=list)
    message: str = ""

    def detail(self) -> dict:
        return {
            "blocks": self.blocks_written,
            "contactBlocks": self.contact_blocks,
            "seconds": round(self.seconds, 1),
            "retries": self.retries,
            "checked": self.checked,
            "mismatches": self.mismatches,
            "first": self.first_mismatches[:VERIFY_REPORT_LIMIT],
            "client": "desktop",
        }


# ── read-back ────────────────────────────────────────────────────────────────
def _read_block(link: SerialLink, addr: int, length: int = READ_LL) -> bytes:
    """One R-frame: TX 52 addr4 LL -> RX **57** addr4 LL data CK 06.

    The REQUEST opcode is 0x52 and the REPLY opcode is 0x57 — the same byte the
    host uses to WRITE. That asymmetry is the radio's, not a typo: it is what
    tools/d890_read.py has always parsed, and a live D168UV read confirms it
    (2026-09-05). Expecting 0x52 back made every verify fail on hardware while a
    device double that echoed 0x52 passed, which is precisely the shape of test
    that proves the author's assumption instead of the protocol.

    Retried on a bad/short reply exactly like a write is: a garbled read must
    never be reported as a MISMATCH, because that would archive a good write as
    a failure and tell the operator their radio is wrong when it is not.
    """
    req = b"\x52" + (addr & 0xFFFFFFFF).to_bytes(4, "big") + bytes([length])
    last = ""
    for attempt in range(1, READ_ATTEMPTS + 1):
        if attempt > 1:
            link.flush()
            time.sleep(0.02)
        link.send(req)
        try:
            head = link.read_exactly(6, READ_TIMEOUT_MS)
            if head[0] != READ_REPLY_OPCODE:
                last = f"no 0x{READ_REPLY_OPCODE:02x} reply header (" + head.hex() + ")"
                continue
            r_addr = int.from_bytes(head[1:5], "big")
            r_len = head[5]
            if r_addr != (addr & 0xFFFFFFFF):
                last = f"address mismatch: asked 0x{addr:08x}, got 0x{r_addr:08x}"
                continue
            if r_len != length:
                last = f"length mismatch: asked {length}, got {r_len}"
                continue
            body = link.read_exactly(r_len + 2, READ_TIMEOUT_MS)
            data, ck, end = body[:r_len], body[r_len], body[r_len + 1]
            if end != ACK:
                last = "the reply did not end in 06"
                continue
            if ck != ce.checksum(addr, data):
                last = "checksum mismatch"
                continue
            return bytes(data)
        except AbortedError:
            raise
        except FirmwareUpdateError as e:
            last = str(e)
    raise CodeplugWriteError(f"could not read back 0x{addr:08x} after {READ_ATTEMPTS} attempts ({last})")


# ── reconnecting across a commit ─────────────────────────────────────────────
def _port_names() -> set:
    try:
        from serial.tools import list_ports
        return {p.device for p in list_ports.comports()}
    except Exception:  # noqa: BLE001 — enumeration is a convenience, never a gate
        return set()


def _wait_for_port(preferred: str, before: set, on_log, abort: Optional[threading.Event],
                   settle_s: Optional[float] = None) -> str:
    """Wait out the reboot and return the port to reopen.

    Prefers the name we were using; if the radio came back under a new name
    (macOS does this), takes the one that appeared while we waited.
    """
    # Read the module constant at CALL time, not at import time, so a caller (or
    # a test) that shortens the reboot wait is actually obeyed.
    settle_s = REBOOT_WAIT_S if settle_s is None else settle_s
    deadline = time.monotonic() + settle_s + REOPEN_TIMEOUT_S
    end_of_settle = time.monotonic() + settle_s
    while time.monotonic() < end_of_settle:
        if abort is not None and abort.is_set():
            raise AbortedError("aborted by the operator while the radio was rebooting")
        left = int(end_of_settle - time.monotonic()) + 1
        on_log(f"radio rebooting — reconnecting in {left}s…", "info")
        time.sleep(1.0)
    while time.monotonic() < deadline:
        if abort is not None and abort.is_set():
            raise AbortedError("aborted by the operator while the radio was rebooting")
        now = _port_names()
        if preferred in now:
            return preferred
        fresh = sorted(now - before)
        if fresh:
            on_log(f"the radio came back as {fresh[0]} (it was {preferred})", "info")
            return fresh[0]
        time.sleep(REOPEN_POLL_S)
    raise CodeplugWriteError(
        f"the radio did not reappear on {preferred} after its reboot. Its codeplug IS written; "
        "reconnect it and read it back from the web app to verify.")


def _session(port: str, on_log, abort, attempts: int = 4) -> SerialLink:
    """Open the port and enter PC mode, retrying a handshake that reads junk.

    A radio that has just rebooted (which is what END causes, and every phase
    here follows one) emits a short banner, and opening the port can nudge it
    into emitting more — AFTER the flush that was meant to clear it. The first
    PROGRAM then reads the banner instead of "QX"+ACK. Measured on a D878UVII:
    the reply came back as 1e 00 20, reproducibly, byte for byte.

    So one flush before one handshake is not enough. Re-open, re-flush and ask
    again: PROGRAM is idempotent, an unanswered one costs nothing, and the
    alternative is telling the operator their radio is not in PC mode when it is
    merely still waking up.
    """
    last: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        link = SerialLink(port, on_log=on_log, abort=abort)
        try:
            link.open(BAUD, dtr=True, rts=True)
            # Let the port settle before clearing it: flushing the instant it
            # opens throws away nothing and leaves the banner still to come.
            time.sleep(OPEN_SETTLE_S)
            link.flush()
            ce._enter_program(link, on_log)
            return link
        except AbortedError:
            try:
                link.close()
            except BaseException:  # noqa: BLE001
                pass
            raise
        except (ce.NotInPcModeError, FirmwareUpdateError, OSError) as e:
            last = e
            try:
                link.close()
            except BaseException:  # noqa: BLE001
                pass
            if attempt < attempts:
                on_log(f"PC mode not entered ({e}); retrying {attempt + 1}/{attempts}", "info")
                time.sleep(RETRY_SETTLE_S)
    raise last if last is not None else CodeplugWriteError("could not open a session")


# ── the write ────────────────────────────────────────────────────────────────
def write_codeplug(port_name: str,
                   codeplug: Iterable[seg.Segment],
                   contacts: Iterable[seg.Segment],
                   on_log: Callable[[str, str], None],
                   on_progress: Callable[[int, int, str], None],
                   on_phase: Optional[Callable[[str, int, int], None]] = None,
                   abort: Optional[threading.Event] = None,
                   ident_tokens=None,
                   on_ident: Optional[Callable[[object], None]] = None,
                   verify: bool = True) -> WriteResult:
    """Write, commit, and (unless asked not to) verify a whole codeplug.

    `on_ident(ident)` fires once, as soon as the radio has said what it is, so a
    caller's log can stop guessing and name it.

    `on_phase(phase, done, total)` is the caller's hook for the server
    heartbeat; it is called at most every HEARTBEAT_EVERY_S so a slow network
    cannot pace the serial loop. Returns a WriteResult whose `outcome` is the
    server's own vocabulary. Raises only when NOTHING was committed — once the
    codeplug is on the radio, every path returns a result instead, because the
    operator needs to be told what state their radio is in.
    """
    code = list(codeplug)
    cont = list(contacts)
    total = seg.block_count(code)
    if total == 0:
        raise CodeplugWriteError("the prepared codeplug is empty — nothing to write")

    # Starts as a failure: every path has to earn its way up from "nothing
    # reached the radio".
    res = WriteResult(outcome="write_failed")
    t0 = time.monotonic()
    beat = _Beat(on_phase)
    before = _port_names()
    link: Optional[SerialLink] = None
    committed = False

    try:
        # ── phase 1: the codeplug ──────────────────────────────────────────
        on_progress(0, total, "handshake")
        link = _session(port_name, on_log, abort)
        ident = ce._read_ident(link, on_log)
        if on_ident is not None:
            on_ident(ident)   # so the log can say WHICH radio from here on
        if not _model_matches(ident_tokens, ident.model):
            raise CodeplugWriteError(
                f'this radio identifies as "{ident.model}" but the codeplug was built for '
                f'{" / ".join(ident_tokens)} — WRONG RADIO. Nothing was written.')
        # The band plan is NOT checked and NOT changed: the radio validates its
        # own channels, and the web app has already shown the operator whether
        # the plans differ. Refusing here would only move a warning they have
        # already seen into a dead end.
        link.flush()
        on_log(f"writing the codeplug: {total:,} blocks ({seg.describe(code)})", "ok")
        res.retries += _write_all(link, code, total, on_progress, beat, "codeplug")
        res.blocks_written = total
        on_progress(total, total, "commit")
        beat.force("commit", total, total)
        ce._exit_program(link, on_log)
        committed = True
        link.close()
        link = None

        # ── phase 2: the digital contact database ──────────────────────────
        if cont:
            ctotal = seg.block_count(cont)
            port_name = _wait_for_port(port_name, before, on_log, abort)
            link = _session(port_name, on_log, abort)
            ce._read_ident(link, on_log)
            link.flush()
            on_log(f"writing the digital contact list: {ctotal:,} blocks — this takes a while", "ok")
            res.retries += _write_all(link, cont, ctotal, on_progress, beat, "contacts")
            res.contact_blocks = ctotal
            beat.force("contacts-commit", ctotal, ctotal)
            ce._exit_program(link, on_log)
            link.close()
            link = None

        # ── phase 3: read-back verify ──────────────────────────────────────
        res.seconds = time.monotonic() - t0
        if not verify:
            res.outcome = "success"
            res.message = "Written and committed (verify skipped)."
            return res
        try:
            port_name = _wait_for_port(port_name, before, on_log, abort)
            link = _session(port_name, on_log, abort)
            ce._read_ident(link, on_log)
            link.flush()
            _verify(link, code, total, on_progress, beat, res)
            try:
                ce._exit_program(link, on_log)
            except FirmwareUpdateError:
                pass        # the verify already happened; leaving PC mode is cosmetic
            link.close()
            link = None     # closed cleanly: the finally block must not END again
        except AbortedError:
            raise
        except (FirmwareUpdateError, OSError) as e:
            # Committed but unverifiable. NOT a failure: the radio commonly drops
            # its port after a commit, and calling that a bad write would archive
            # a good one and frighten the operator for no reason.
            res.outcome = "verify_unavailable"
            res.message = f"Written and committed, but the radio could not be read back ({e})."
            on_log(res.message, "er")
            return res

        if res.mismatches == 0:
            res.outcome = "success"
            res.message = f"Written, committed and verified ({res.checked:,} blocks read back)."
            on_log(res.message, "ok")
        else:
            res.outcome = "verify_failed"
            res.message = (f"Committed, but {res.mismatches:,} of {res.checked:,} blocks read back "
                           "differently. Write it again before using the radio.")
            on_log(res.message, "er")
        return res

    except AbortedError:
        # An abort AFTER the commit still leaves a written radio; say so rather
        # than pretending nothing happened.
        res.outcome = "verify_unavailable" if committed else "write_failed"
        res.message = ("Stopped by the operator after the codeplug was committed — the radio IS written "
                       "but was not verified." if committed else
                       "Stopped by the operator before anything was committed — the radio is unchanged.")
        return res
    except (FirmwareUpdateError, OSError) as e:
        res.outcome = "verify_unavailable" if committed else "write_failed"
        res.message = (f"The codeplug was committed, then the link failed ({e}). Verify the radio "
                       "before using it." if committed else f"The write failed before anything was committed ({e}).")
        return res
    finally:
        if link is not None:
            # Never leave the radio in PC mode holding an uncommitted session.
            try:
                link.abort = None
                ce._exit_program(link, on_log, timeout_ms=2000)
            except BaseException:  # noqa: BLE001
                pass
            try:
                link.close()
            except BaseException:  # noqa: BLE001
                pass
        res.seconds = res.seconds or (time.monotonic() - t0)


def _model_matches(tokens, reported: str) -> bool:
    """Does the radio in front of us identify as one of the tokens the server
    said this codeplug belongs to?

    EXACT, case-insensitive comparison, never a substring: a generation-1
    D578UV/D878UV answers with a strict prefix of its generation-2 sibling's
    token, and writing a gen-2 codeplug into one would be the worst kind of
    "close enough". An empty token list means the server did not say, which is
    "do not enforce" — not "refuse everything".

    The project model id ("anytone_d890") is deliberately NOT used: it has no
    reliable relationship to the identity token ("D890UV"), and a rebadged radio
    answers with its own name entirely ("DMR-7X2").
    """
    if isinstance(tokens, str):
        tokens = [tokens]
    tokens = [t.strip().lower() for t in (tokens or []) if isinstance(t, str) and t.strip()]
    if not tokens:
        return True
    return (reported or "").strip().lower() in tokens


class _Beat:
    """Rate-limits the caller's heartbeat so the network cannot pace the wire."""

    def __init__(self, on_phase):
        self.on_phase = on_phase
        self.last = 0.0

    def maybe(self, phase: str, done: int, total: int) -> None:
        if self.on_phase is None:
            return
        now = time.monotonic()
        if now - self.last >= HEARTBEAT_EVERY_S:
            self.last = now
            self.on_phase(phase, done, total)

    def force(self, phase: str, done: int, total: int) -> None:
        if self.on_phase is not None:
            self.last = time.monotonic()
            self.on_phase(phase, done, total)


def _write_all(link: SerialLink, segments, total: int, on_progress, beat: _Beat,
               phase: str) -> int:
    """Every block of `segments`, in address order. Returns the resend count."""
    retries = 0
    written = 0
    for addr, data in seg.iter_blocks(segments):
        link.check_abort()
        attempts = ce._write_block(link, addr, data, link.on_log)
        retries += attempts - 1
        written += 1
        if (written % PROGRESS_EVERY_BLOCKS) == 0 or written == total:
            on_progress(written, total, phase)
            beat.maybe(phase, written, total)
    return retries


def _verify(link: SerialLink, segments, total: int, on_progress, beat: _Beat,
            res: WriteResult) -> None:
    """Read every written block back and compare. Records mismatches rather
    than raising: the caller reports them as an outcome."""
    checked = 0
    for addr, data in seg.iter_blocks(segments):
        link.check_abort()
        got = _read_block(link, addr, len(data))
        checked += 1
        if got != data:
            res.mismatches += 1
            if len(res.first_mismatches) < VERIFY_REPORT_LIMIT:
                res.first_mismatches.append({
                    "addr": f"{addr:08x}",
                    "wrote": bytes(data).hex(),
                    "read": got.hex(),
                })
        if (checked % PROGRESS_EVERY_BLOCKS) == 0 or checked == total:
            on_progress(checked, total, "verify")
            beat.maybe("verify", checked, total)
    res.checked = checked
