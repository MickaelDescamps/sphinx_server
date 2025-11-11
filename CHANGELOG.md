# Changelog

All notable changes to `sphinx-server` are documented here.

The project adheres to [Semantic Versioning](https://semver.org/) and to [keep a changelog project](https://keepachangelog.com/en/1.1.0/)

## [Unreleased]
### Added

### Changed

### Removed

### Deprecated

### Fixed

### Security


## [0.1.3] 2025-11-11

### Added
- build start time in admin list of build
- lot of logs
- optional pyenv-based build environments via `SPHINX_SERVER_ENV_MANAGER`
- per-target selection of uv or pyenv for each tracked target
- Admin settings page that edits core config values and persists them to `.env`
- Role-based authentication with login/logout, session cookies, viewer/contributor/administrator permissions, a self-service account page, and an admin-only user management screen (session protection remains configurable through `.env`).
- Forced password changes on first login (bootstrap `admin` / `password` account and admin-issued resets now require updating credentials before accessing other pages).
- Support detecting docs/dev extras declared via Poetry v1 (`[tool.poetry.extras]`) and Poetry dependency groups (`[tool.poetry.group.<name>.dependencies]`) so those optional dependencies are installed automatically during builds.
- pyenv builds now respect the Python version declared in `pyproject.toml` (`project.requires-python` or Poetry’s `python` dependency) before falling back to `.python-version` or the global default.
- Repository-level toggle to expose documentation publicly; artifact downloads now share the same access control, so private repos stay hidden unless a user signs in.

### Changed
- display time in local time

### Removed

- `SPHINX_SERVER_DEFAULT_ADMIN_USERNAME` / `SPHINX_SERVER_DEFAULT_ADMIN_PASSWORD` environment variables (the bootstrap admin is always `admin` / `password` and is flagged for a mandatory password change).

### Fixed
- Missing `itsdangerous` dependency required by the new session middleware.


## [0.1.2] - 2025-11-10
Missing CHANGELOG


## [0.1.1] - 2025-11-09

### Changed
- Include build date in documentation
- Fix user panel css bug


## [0.1.0] - 2025-11-09


### Added
- FastAPI control panel that boots the administrator UI and docs explorer, wires in the build queue plus auto-build monitor, and serves published artifacts directly under `/artifacts`.
- Administrator dashboard for registering repositories with provider metadata, docs paths, personal access tokens, or SSH deploy keys; manage tracked branches/tags, trigger builds manually, bulk delete/build, and mark a primary ref whose metadata (name/version/summary/homepage) is surfaced to readers.
- Git integration layer that clones/fetches repositories with token injection or ephemeral SSH keys, lists available remote refs, and records the latest SHA per tracked target so rebuilds only fire when necessary.
- Build execution pipeline that provisions isolated uv-managed virtual environments per job, installs `sphinx` plus project doc/dev extras or requirements files, runs `sphinx-build`, captures logs, measures durations, and injects a lightweight navigation script into every generated HTML page.
- Background `AutoBuildMonitor` that polls remote refs via `git ls-remote`, skips duplicates when builds are already queued/running, and enqueues fresh builds whenever tracked branches/tags advance.
- Public documentation explorer that lists every onboarded repository, highlights the latest successful artifact per tracked ref, and exposes JSON endpoints for embedding build/ref metadata in other UIs.
- CLI entry point (`sphinx-server`) powered by uvicorn plus `.env`-driven configuration for host/port, workspace/layout directories, database URL, process pool size, Git/Sphinx timeouts, and other operational knobs.
