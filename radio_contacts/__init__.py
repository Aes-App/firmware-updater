"""The "Digital Contact Refresh" tab: replace a radio's digital-contact database
with one of the AesApp server's daily-built lists.

The app is opened from the Tools page on cps.aes.app through an
``aesapp://contacts?token=…`` link (radio_contacts.launch). The token buys a
short server session (radio_contacts.catalog) that lists the prebuilt contact
bundles and serves their artifacts; the artifact is the exact block stream the
factory CPS sends, so the app never encodes a contact — it verifies the
download (sha256), decodes the container (radio_contacts.segments) and streams
the 16-byte blocks over the codeplug PC-mode protocol (radio_contacts.engine).
"""
