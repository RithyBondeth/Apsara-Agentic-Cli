# Releasing Apsara Agentic

Apsara ships as the `apsara-agentic` Python package and is installed with
`pipx`. There is no npm launcher to coordinate or maintain.

Alpha builds use PEP 440 prerelease versions such as `0.1.0a1`; reserve
`0.1.0` for the final release.

## 0. Pre-flight

```bash
cd Apsara-Agentic-Cli
python -m pip install -e ".[dev,release]"
python -m pytest            # all tests green
python scripts/pipx_smoke.py # after `python -m build --wheel`
apsara doctor --live        # environment and selected provider are healthy
git diff --check
```

## 1. Build & validate the Python package

```bash
python -m build             # builds sdist + wheel into dist/
twine check dist/*          # validates metadata + README render
python -m pip_audit         # no known dependency advisories
shasum -a 256 dist/*        # save these hashes in the GitHub release
```

Sanity-check that the wheel contains only `apsara_cli/**` and the right entry
point (`apsara = apsara_cli.cli:main`).

## 2. (Recommended) Smoke-test on TestPyPI first

```bash
twine upload --repository testpypi dist/*
python -m venv /tmp/apsara-testpypi
/tmp/apsara-testpypi/bin/pip install --extra-index-url https://pypi.org/simple \
  --index-url https://test.pypi.org/simple apsara-agentic==<version>
/tmp/apsara-testpypi/bin/apsara --version
/tmp/apsara-testpypi/bin/apsara --help
```

## 3. Publish to PyPI

Requires a PyPI account with an API token (set `TWINE_USERNAME=__token__` and
`TWINE_PASSWORD=<token>`, or use `~/.pypirc`).

```bash
twine upload dist/*
```

Verify from a new environment before tagging:

```bash
python -m venv /tmp/apsara-pypi
/tmp/apsara-pypi/bin/pip install apsara-agentic==<version>
/tmp/apsara-pypi/bin/apsara doctor --no-live --no-color
```

CI repeats the complete `pipx` lifecycle on macOS, Linux, and Windows with the
oldest and newest supported Python versions: wheel install, first-run `init`,
missing-provider/optional-dependency guidance, upgrade, and uninstall.

## 4. Tag and announce the release

```bash
git tag -a v<version> -m "Apsara Agentic v<version>"
git push origin v<version>
```

Create a GitHub release from the tag. Attach the wheel, source archive, and
SHA-256 checksums. Then verify the public install path:

```bash
pipx install apsara-agentic==<version>
apsara --version
apsara doctor --no-live
```

## Rollback

PyPI release files are immutable. Do not attempt to overwrite a broken
version.

1. Yank the affected version on PyPI so new installers do not select it.
2. Mark the GitHub release as withdrawn and put the reason at the top.
3. Revert the release commit on a new branch.
4. Fix and publish a higher patch version.
5. Un-yank only if the original files are proven safe and compatible.

Rollback immediately if installation fails, credentials are exposed, a built-in
file tool crosses the workspace boundary, background processes survive shutdown,
or the live provider probe fails consistently.

## Notes

- `dist/` and `build/` are gitignored; never commit build artifacts.
- Publish PyPI before deploying website copy that advertises the public `pipx`
  command.
