# Development

## Running the tests

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest
```

This installs the latest Home Assistant release. To test against the oldest supported release, the `homeassistant` version in `hacs.json`:

```sh
.venv/bin/pip install -r requirements_test.txt "homeassistant==$(jq -r .homeassistant hacs.json)"
```

CI runs both, plus [hassfest](https://developers.home-assistant.io/blog/2020/04/16/hassfest/), HACS validation, and ruff.

## Releasing

Releases are made from `main`. Publish a GitHub release with a tag like `v0.1.0`. The release workflow writes that version into `manifest.json` and attaches `unavailable_aware.zip`, which is what HACS installs. The `0.0.0` in the repository is a placeholder; don't edit it.

HACS installs releases, never the `main` branch: `hide_default_branch` in `hacs.json` stops it from offering `main`, which would install unreleased code with the placeholder version.

### Pre-releases

Tag pre-releases the way Home Assistant does: `v0.1.0b1`, then `v0.1.0b2`, `v0.1.0rc1`, and so on. Mark the release as a pre-release (`gh release create v0.1.0b1 --prerelease`). HACS only offers pre-releases to users who turn on **Show beta versions** for the repository. Once a pre-release has held up, publish the full release (`v0.1.0`) from the same commit.

### Fixing a release while `main` has unreleased changes

Create a branch such as `release/0.1.x` from the release's tag, make the fix there, and release from that branch. Then bring the fix into `main` as well.
