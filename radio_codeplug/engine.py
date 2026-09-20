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
READ_REPLY_OPCODE = 0x57
READ_TIMEOUT_MS = 1500
READ_ATTEMPTS = 5
REBOOT_WAIT_S = 30
OPEN_SETTLE_S = 1.5
RETRY_SETTLE_S = 3.0
REOPEN_TIMEOUT_S = 60
REOPEN_POLL_S = 1.0
PROGRESS_EVERY_BLOCKS = 256
HEARTBEAT_EVERY_S = 20.0
VERIFY_REPORT_LIMIT = 8


class CodeplugWriteError(ce.ContactWriteError):
    pass


@dataclass
class WriteResult:
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


def _read_block(link: SerialLink, addr: int, length: int = READ_LL) -> bytes:
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


def _port_names() -> set:
    try:
        from serial.tools import list_ports
        return {p.device for p in list_ports.comports()}
    except Exception:
        return set()


def _wait_for_port(preferred: str, before: set, on_log, abort: Optional[threading.Event],
                   settle_s: Optional[float] = None) -> str:
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
    last: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        link = SerialLink(port, on_log=on_log, abort=abort)
        try:
            link.open(BAUD, dtr=True, rts=True)
            time.sleep(OPEN_SETTLE_S)
            link.flush()
            ce._enter_program(link, on_log)
            return link
        except AbortedError:
            try:
                link.close()
            except BaseException:
                pass
            raise
        except (ce.NotInPcModeError, FirmwareUpdateError, OSError) as e:
            last = e
            try:
                link.close()
            except BaseException:
                pass
            if attempt < attempts:
                on_log(f"PC mode not entered ({e}); retrying {attempt + 1}/{attempts}", "info")
                time.sleep(RETRY_SETTLE_S)
    raise last if last is not None else CodeplugWriteError("could not open a session")


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
    code = list(codeplug)
    cont = list(contacts)
    total = seg.block_count(code)
    if total == 0:
        raise CodeplugWriteError("the prepared codeplug is empty — nothing to write")

    res = WriteResult(outcome="write_failed")
    t0 = time.monotonic()
    beat = _Beat(on_phase)
    before = _port_names()
    link: Optional[SerialLink] = None
    committed = False

    try:
        on_progress(0, total, "handshake")
        link = _session(port_name, on_log, abort)
        ident = ce._read_ident(link, on_log)
        if on_ident is not None:
            on_ident(ident)
        if not _model_matches(ident_tokens, ident.model):
            raise CodeplugWriteError(
                f'this radio identifies as "{ident.model}" but the codeplug was built for '
                f'{" / ".join(ident_tokens)} — WRONG RADIO. Nothing was written.')
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
                pass
            link.close()
            link = None
        except AbortedError:
            raise
        except (FirmwareUpdateError, OSError) as e:
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
            try:
                link.abort = None
                ce._exit_program(link, on_log, timeout_ms=2000)
            except BaseException:
                pass
            try:
                link.close()
            except BaseException:
                pass
        res.seconds = res.seconds or (time.monotonic() - t0)


def _model_matches(tokens, reported: str) -> bool:
    if isinstance(tokens, str):
        tokens = [tokens]
    tokens = [t.strip().lower() for t in (tokens or []) if isinstance(t, str) and t.strip()]
    if not tokens:
        return True
    return (reported or "").strip().lower() in tokens


class _Beat:

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
