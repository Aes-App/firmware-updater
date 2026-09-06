"""The codeplug write session: the link, the job, and how a downloaded write
plan is split into its two phases.

The property under test throughout is that the app never GUESSES about the
radio's memory. Where the digital contact database lives differs between radio
families, so the split comes from the server; a plan that says it carries
contacts without saying where they are is refused rather than written in one
phase, because that is the exact corruption the two-phase write exists to avoid.

Run:  python -m pytest tests/test_codeplug_client.py -q
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radio_contacts import launch                      # noqa: E402
from radio_codeplug import client                      # noqa: E402

TOKEN = "AbCdEf0123456789_-xyz"
BLOCK = "00" * 16


def seg_dict(addr: int, blocks: int = 1) -> dict:
    return {"addr": f"{addr:08x}", "hex": BLOCK * blocks}


# ── the link ────────────────────────────────────────────────────────────────
def test_a_codeplug_link_parses_and_keeps_its_action():
    r = launch.parse_launch_url(f"aesapp://codeplug?token={TOKEN}&server=https://cps.aes.app/")
    assert r.action == launch.ACTION_CODEPLUG
    assert r.token == TOKEN
    assert r.base_url("https://fallback.invalid") == "https://cps.aes.app"


def test_a_link_cannot_point_the_app_at_a_stranger():
    r = launch.parse_launch_url(f"aesapp://codeplug?token={TOKEN}&server=https://evil.example")
    assert r.base_url("https://cps.aes.app") == "https://cps.aes.app"


def test_an_unknown_action_is_still_refused():
    with pytest.raises(launch.LaunchError, match="does not handle"):
        launch.parse_launch_url(f"aesapp://firmware?token={TOKEN}")


# ── the write plan ──────────────────────────────────────────────────────────
def test_a_codeplug_only_plan_needs_no_contact_regions():
    plan = client.WritePlan.from_envelope({
        "segments": [seg_dict(0x02500000, 2), seg_dict(0x00800000)],
        "contactsIncluded": False,
        "stats": {"channels": 12},
    })
    assert [s.addr for s in plan.codeplug] == [0x00800000, 0x02500000]   # address-ascending
    assert plan.contacts == []
    assert plan.blocks == 3
    assert plan.stats["channels"] == 12


def test_the_split_comes_from_the_servers_regions():
    plan = client.WritePlan.from_envelope({
        "segments": [seg_dict(0x02500000), seg_dict(0x04000000), seg_dict(0x05500000)],
        "contactsIncluded": True,
        "contactRegions": [[0x04000000, 0x04800000], [0x04840000, 0x0A000000]],
    })
    assert [s.addr for s in plan.codeplug] == [0x02500000]
    assert [s.addr for s in plan.contacts] == [0x04000000, 0x05500000]


def test_the_890s_own_regions_keep_its_nx_metadata_in_the_codeplug():
    # 0x18000000..0x1818007f are NX cov/metadata tables, NOT contacts: writing
    # them in the contact phase leaves the radio repairing itself on boot.
    plan = client.WritePlan.from_envelope({
        "segments": [seg_dict(0x18000000), seg_dict(0x18280000), seg_dict(0x07000000)],
        "contactsIncluded": True,
        "contactRegions": [[0x07000000, 0x18000000], [0x18280000, 0x1B000000]],
    })
    assert [s.addr for s in plan.codeplug] == [0x18000000]
    assert [s.addr for s in plan.contacts] == [0x07000000, 0x18280000]


def test_contacts_without_regions_are_refused_not_guessed():
    with pytest.raises(client.CodeplugClientError, match="where the digital contact list lives"):
        client.WritePlan.from_envelope({
            "segments": [seg_dict(0x02500000), seg_dict(0x04000000)],
            "contactsIncluded": True,
        })


def test_a_plan_that_claims_contacts_but_has_none_is_refused():
    with pytest.raises(client.CodeplugClientError, match="none of the prepared data"):
        client.WritePlan.from_envelope({
            "segments": [seg_dict(0x02500000)],
            "contactsIncluded": True,
            "contactRegions": [[0x04000000, 0x04800000]],
        })


@pytest.mark.parametrize("segments, hint", [
    ([], "empty"),
    ([{"addr": "02500000", "hex": "00" * 15}], "not whole 16-byte blocks"),
    ([{"addr": "02500000", "hex": "zz" * 16}], "not readable hex"),
    ([{"addr": 0x02500000, "hex": BLOCK}], "malformed"),
    ([{"addr": "02500000"}], "malformed"),
])
def test_a_short_or_malformed_download_never_becomes_a_write(segments, hint):
    with pytest.raises(client.CodeplugClientError, match=hint):
        client.WritePlan.from_envelope({"segments": segments})


def test_a_broken_region_list_is_refused():
    for regions in ([[5]], [["a", "b"]], [[9, 9]], "nope"):
        with pytest.raises(client.CodeplugClientError, match="contact-region list"):
            client.WritePlan.from_envelope({
                "segments": [seg_dict(0x02500000)], "contactRegions": regions,
            })


# ── the session's refusals ──────────────────────────────────────────────────
class _FakeRequest:
    """Stands in for radio_contacts.catalog._request: returns canned replies."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, token, timeout, **kw):
        self.calls.append((method, url, kw.get("payload"), kw.get("retry", True)))
        status, body = self.replies.pop(0)
        return status, json.dumps(body).encode()


def _session(monkeypatch, *replies) -> tuple:
    fake = _FakeRequest(*replies)
    monkeypatch.setattr("radio_contacts.catalog._request", fake)
    return client.CodeplugSession("https://cps.aes.app", TOKEN), fake


def test_claim_reads_the_job_out_of_the_session(monkeypatch):
    sess, _ = _session(monkeypatch, (200, {
        "ok": True, "jobId": "abc123", "leaseSeconds": 600,
        "project": {"id": 7, "uuid": "u", "name": "Home", "model": "anytone_d878uv2"},
        "stats": {"blocks": 5},
    }))
    job = sess.claim()
    assert job.job_id == "abc123" and job.project_id == 7
    assert job.model == "anytone_d878uv2" and job.lease_seconds == 600


def test_a_lost_lease_is_its_own_error_so_the_write_stops(monkeypatch):
    sess, _ = _session(monkeypatch, (409, {"error": "lock_lost", "message": "The write lock expired."}))
    with pytest.raises(client.LockLostError):
        sess.claim()


def test_a_refused_token_says_what_to_do(monkeypatch):
    sess, _ = _session(monkeypatch, (410, {"error": "token_expired"}))
    with pytest.raises(client.CodeplugClientError, match="expired"):
        sess.claim()


def test_a_heartbeat_does_not_retry_and_survives_a_dropped_network(monkeypatch):
    import radio_contacts.catalog as cat

    def boom(method, url, token, timeout, **kw):
        assert kw.get("retry") is False, "a heartbeat that backs off for 30s has already failed its job"
        raise cat.ContactsError("network down")

    monkeypatch.setattr("radio_contacts.catalog._request", boom)
    sess = client.CodeplugSession("https://cps.aes.app", TOKEN)
    assert sess.heartbeat("codeplug", 1, 2) is False, "a missed beat must not stop a write"


def test_a_lost_lease_during_a_heartbeat_does_stop_the_write(monkeypatch):
    sess, _ = _session(monkeypatch, (409, {"error": "lock_lost"}))
    with pytest.raises(client.LockLostError):
        sess.heartbeat("codeplug", 1, 2)


def test_reporting_the_outcome_never_raises(monkeypatch):
    import radio_contacts.catalog as cat

    monkeypatch.setattr("radio_contacts.catalog._request",
                        lambda *a, **k: (_ for _ in ()).throw(cat.ContactsError("no network")))
    sess = client.CodeplugSession("https://cps.aes.app", TOKEN)
    # A radio that WAS written must not be reported as failed just because the
    # report could not be delivered.
    assert sess.report(client.OUTCOME_SUCCESS) is False
