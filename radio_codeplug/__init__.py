"""Write a codeplug prepared on the web app to a radio over USB.

The web app builds the write data — it owns the codec — and hands this app a
one-time link. We claim it, download the prepared segments, write them at full
serial speed, verify by reading back, and report how it went. The project stays
read-only on the server for as long as we keep saying we are working on it.
"""
from .client import CodeplugSession, CodeplugClientError, LockLostError   # noqa: F401
