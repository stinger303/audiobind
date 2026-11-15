# Security policy

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Report it privately through GitHub:
on the repository's **Security** tab choose **Report a vulnerability**
(<https://github.com/stinger303/audiobind/security/advisories/new>). Include the version
(`docker exec audiobind audiobind version`), what you did, and what happened. Do not paste a
`mam_id` cookie, a password or a session cookie into a report.

## In scope

- The web UI and its API: sign-in, sessions, the host and origin checks, `/healthz`.
- The MyAnonamouse client: what it sends, where it sends it, and the egress and proxy guards.
- Path handling: anything that lets a book, an ingest file or a request read, write or delete
  outside `/library`, `/output`, `/ingest` and `/config`.

## Out of scope

- A misconfigured container: a port published to the internet, `ALLOWED_HOSTS` or
  `TRUSTED_PROXY_IPS` set too wide, a writable library mount you meant to be read-only.
- A hostile admin: the one account can already delete books and change every setting.

## Supported versions

Only the latest release gets fixes.

## Where to run it

Audiobind is LAN-first. Do not expose it to the internet without a reverse proxy and TLS in
front of it (see "Behind a reverse proxy" in the README).
