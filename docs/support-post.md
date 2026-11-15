# Unraid forum support post

Title: **Audiobind - merge, verify and tag audiobook libraries**

---

I built this for my own mess: one book spread over 195 files, another across nine "discs", and a
folder that turned out to be fourteen books at once.

Audiobind points at your audiobook folder, works out what each book actually is, checks it against
the Audible catalog, and merges the parts into one properly tagged `.m4b` with real chapter marks
and cover art. Then it files it as `Author/Book/Book.m4b`, or `Author/Series/3 - Title/Title.m4b`,
with an Audiobookshelf `metadata.json` beside it.

It's deliberately cautious. It compares runtime against the catalog, so a set that's an hour short
gets reported instead of merged, and anything it isn't sure about goes to a Review list and waits
for you. Your source files are never changed — mount the library read-only and it can't touch them
at all.

Works with Audiobookshelf, Plex, Emby, Jellyfin and Apple Books.

There's an optional wishlist watcher for a private tracker plus qBittorrent, but you don't need it.
It converts whatever lands in the ingest folder, from any downloader or a plain copy.

First run is short: set a library path and an output path, create an account, hit Scan.

Install from Community Applications once this is through review — until then the template link in
the repo works. Bug reports and feature requests: reply here.
