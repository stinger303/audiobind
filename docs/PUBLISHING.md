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

After tagging, bump `templates/audiobind.xml`: set the `<Repository>` tag to the new version
(`ghcr.io/stinger303/audiobind:0.2.0`) and add the version to `<Changes>`. The template ships
pinned, never `:latest`, because Unraid reuses a matching local tag instead of pulling, so a
`:latest` install keeps whatever build it first fetched. Pinning means the template is what
announces a release.

Pushing may need a deploy key that is authorized for this repo (the default key of your
account may not be). Point git at it for the push only, with a path of your own:

```bash
GIT_SSH_COMMAND='ssh -i <path-to-deploy-key> -o IdentitiesOnly=yes' \
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

## 3. One-time: support link

Community Applications requires a real `<Support>` URL — a placeholder fails the scan with
"Starter defaults removed". Either works:

- **GitHub issues** (what the template ships today): `https://github.com/stinger303/audiobind/issues`
- **A Unraid forum thread**: create a topic in **Docker Containers** titled `Audiobind - merge,
  verify and tag audiobook libraries`, paste the text from `docs/support-post.md`, then put the
  resulting URL into `templates/audiobind.xml` → `<Support>`.

`ca_profile.xml` → `<Forum>` is optional. Leave it out unless a stable thread exists — a placeholder
there fails the same scan.

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
