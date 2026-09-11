"""The "Digital Contact Refresh" tab: replace a radio's digital-contact database
with one of the AesApp server's daily-built lists, or with one built here.

THE PREFERRED PATH IS STILL THE SERVER'S. The app is opened from My Contact
Lists on cps.aes.app -- the page the list was uploaded to -- through an
``aesapp://contacts?token=…`` link (see radio_contacts.launch). The token buys
a short server session (radio_contacts.catalog) that lists the prebuilt contact
bundles and serves their artifacts; an artifact **is the exact block stream the
factory CPS sends**, so on that path the app encodes nothing — it verifies the
download (sha256), decodes the container (radio_contacts.segments) and streams
the 16-byte blocks over the codeplug PC-mode protocol (radio_contacts.engine).

THE SECOND PATH ENCODES HERE. An operator with a register download of their own
(RadioID's user.csv, and optionally nxdn.csv) can build the same database on
their machine — no link, no token, nothing fetched or uploaded — by picking the
countries they want. Those bytes are produced by radio_contacts.contact_build,
which is a port of the encoders behind the server's own bundles and is pinned
byte-for-byte against them in tests/test_contacts_local_build.py: the same input
has to give the same database whichever built it, or that test fails.
"""
