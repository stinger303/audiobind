# Publishing Audiobind

Everything a maintainer needs to ship a release and keep the Community Applications
submission healthy.

## 1. Cut a release

```bash
# bump audiobind/__init__.py __version__ and CHANGELOG.md first
git add -A
git commit -m "Release v0.2.0"
git tag -a v0.2.0 -m "v0.2.0"
git push origin main --tags
```

Pushing needs the deploy key - the default `~/.ssh` key is not authorized for
this repo, so prefix the push (or the whole release) with:

```bash
GIT_SSH_COMMAND='ssh -i /opt/data/ssh/github_audiobind -o IdentitiesOnly=yes' \
  git push origin main --tags
```

The GitHub Actions workflow builds `ghcr.io/stinger303/audiobind` and pushes:

| Tag pushed | Image tags produced |
|---|---|
| branch `main` | `latest`, `sha-<short>` |
| git tag `v0.1.0` | `0.1.0`, `0.1`, `latest` |

## 2. One-time: make the container package public

GHCR packages start private even when the repo is public.

GitHub -> your profile -> **Packages** -> `audiobind` -> *Package settings* ->
**Change visibility** -> Public.

Without this, `docker pull ghcr.io/stinger303/audiobind:latest` returns 403 and the
Unraid install fails.

## 3. One-time: Unraid forum support thread

Community Applications requires a `<Support>` URL. Create a topic in
**Docker Containers** on the Unraid forums titled `Audiobind - merge, verify and tag
audiobook libraries`, paste the text from `docs/support-post.md`, then put the resulting
URL into both:

- `templates/audiobind.xml` -> `<Support>`
- `ca_profile.xml` -> `<Forum>`

## 4. Submit to Community Applications

1. Go to <https://ca.unraid.net/submit/new> and sign in with your Unraid account.
2. Add `https://github.com/stinger303/audiobind`.
3. Run **Validate**, then **Scan**. Fix anything they report and re-run.
4. **Submit**. A CA moderator reviews it manually; expect a few days.

Scans commonly reject: placeholder icon, missing `ca_profile.xml`, empty `<Profile>`,
missing OSI license, a `<TemplateURL>` that does not resolve to the exact raw file, or a
`<Support>` link that 404s.

## 5. Verify the published template resolves

```bash
curl -s https://raw.githubusercontent.com/stinger303/audiobind/main/templates/audiobind.xml | head -5
curl -sI https://raw.githubusercontent.com/stinger303/audiobind/main/icon.svg | head -1
curl -sI https://raw.githubusercontent.com/stinger303/audiobind/main/docs/screenshot.png | head -1
```

All three must return `200`.
