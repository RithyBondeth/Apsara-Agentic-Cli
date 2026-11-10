# Installation test strategy

The release path is a built wheel installed by `pipx`, not an editable source
checkout. `scripts/pipx_smoke.py` tests that exact boundary in an isolated
`PIPX_HOME` and `PIPX_BIN_DIR`.

## CI matrix

GitHub Actions runs the lifecycle on Ubuntu, macOS, and Windows using Python
3.10 and 3.14, the oldest and newest supported versions. Each job:

1. builds one wheel;
2. installs it with `pipx`;
3. runs version and help entry points;
4. initializes a clean workspace and verifies generated configuration;
5. runs offline doctor checks and verifies actionable missing-provider and
   optional-intelligence guidance;
6. exercises `pipx upgrade` and runs the command again;
7. uninstalls and confirms the environment is absent from `pipx list --json`.

The existing package job separately validates wheel/sdist metadata, performs a
plain virtual-environment install, runs `pip check`, and audits dependencies.

## Local reproduction

```bash
python -m pip install build pipx
python -m build --wheel
python scripts/pipx_smoke.py
```

The script uses temporary directories and does not modify the developer's
normal pipx environments.
