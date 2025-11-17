# Sphinx Server

Sphinx Server is a lightweight control plane that keeps track of documentation repositories hosted on GitHub, GitLab, or any generic Git endpoint. It offers three cooperating pieces inside a single FastAPI deployment:

1. **Administrator UI** – browse to `/admin` to onboard repositories, choose the branches/tags to watch, and trigger builds on demand.
2. **Build worker** – a background queue clones/fetches repositories, checks out the selected refs, and runs `sphinx-build` to generate HTML artifacts.
3. **Documentation explorer** – the public landing page (`/`) lists every tracked repository and exposes the generated Sphinx sites grouped by branch/tag.

## Features
- Register multiple repositories with provider metadata, docs directories, and (optional) access tokens for private clones.
- Provide per-repository SSH deploy keys when HTTPS tokens aren’t available; keys stay scoped to each repo and are used only for its builds. You can also register repositories using SSH remotes (`git@...` or `ssh://...`) to reuse whatever identities/agent your system already trusts, no deploy key required. During installs we also apply the repo’s optional `dev`/`docs` extras so build tooling/plugins are available just like in CI.
- Automatically detect documentation extras declared via PEP 621 (`[project.optional-dependencies]`), Poetry v1 extras (`[tool.poetry.extras]`), or Poetry dependency groups (`[tool.poetry.group.<name>.dependencies]`) so doc/dev tooling is installed for each build.
- When using pyenv-managed builds, the server honors `pyproject.toml` Python requirements (`project.requires-python` or Poetry’s `python` dependency) before falling back to `.python-version` or the global default, ensuring docs build with the expected interpreter.
- Track any number of branches or tags per repository.
- Role-based authentication with viewer / contributor / administrator permissions, including per-user password management, enforced first-login password changes, and an admin user directory.
- Optional LDAP/LDAPS authentication that syncs profile metadata, maps LDAP groups to roles, and defers account management to your directory without storing LDAP users locally.
- Mark repositories as public to expose their documentation/artifacts without signing in, while keeping other repos private behind authentication.
- Edit or delete repositories later and manage tracked branches/tags directly from the administrator UI.
- Kick off builds manually from the administrator UI; the worker logs git + Sphinx output per build and exposes the logs in the browser.
- Clean stale build artifacts/log files from the UI to keep storage tidy.
- Each tracked branch/tag always exposes its latest successful build at a stable URL, while the Sphinx UI gets an embedded selector (like Read the Docs) to hop between other tracked refs without reloading the admin site.
- Designate a "main" tracked target per repository, surface its pyproject metadata (name/version/summary) in the docs explorer, and update the metadata automatically whenever that target is rebuilt.
- Builds run in parallel inside isolated workspaces (fresh git clone + dedicated virtualenv per build), so multiple refs of the same repo can render simultaneously without interfering with each other.
- Pick the Python environment manager (uv or pyenv+pip) per tracked target so docs can build under the toolchain each branch/tag expects.
- Tweak core server settings (host, data directory, timeouts, default env manager, etc.) from the admin **Settings** page; edits persist to the `.env` file.
- Build logs stream live in the admin UI, and each build records its duration so you can see how long docs took to compile.
- An auto-build monitor periodically checks refs marked with "Auto build" and automatically enqueues builds when remote commits advance.
- Build history indicates whether each run was triggered manually or automatically, so you can distinguish user-initiated rebuilds from watcher activity.
- Serve compiled documentation directly from the server under `/artifacts/<repo>/<target>/`.

## Requirements
- Python 3.10+
- Git CLI
- [uv](https://github.com/astral-sh/uv) CLI (used to create repo-specific virtual environments during builds) **or** [pyenv](https://github.com/pyenv/pyenv) when `SPHINX_SERVER_ENV_MANAGER=pyenv`

## Usage
```bash
# Install dependencies in your environment
pip install -e .

# Run the FastAPI stack
sphinx-server  # or python -m sphinx_server.main
```

Browse to `http://localhost:8000/login`, sign in with the bootstrap administrator credentials (`admin` / `password`), and follow the forced password-change prompt on the **My account** page. Contributors can access `/admin` while administrators also see `/admin/settings` and `/admin/users`. After configuring at least one branch/tag target, click **Build now**; the worker clones the repo, provisions a dedicated virtual environment for that repo (uv by default or pyenv+pip when configured, with per-target overrides available in the admin UI), installs dependencies from its `pyproject.toml` or requirements files, and runs `sphinx-build`. Once the build completes successfully the documentation becomes available under `/docs/...` and `/artifacts/...`.

## Configuration
The service is configured through environment variables (prefix `SPHINX_SERVER_`). Useful settings:

| Variable | Description | Default |
| --- | --- | --- |
| `SPHINX_SERVER_HOST` | Bind host | `0.0.0.0` |
| `SPHINX_SERVER_PORT` | Bind port | `8000` |
| `SPHINX_SERVER_RELOAD` | Enable uvicorn reload (dev) | `false` |
| `SPHINX_SERVER_SSL_CERTFILE` / `SPHINX_SERVER_SSL_KEYFILE` | Enable HTTPS by pointing to PEM-encoded cert/key files | unset |
| `SPHINX_SERVER_SSL_KEYFILE_PASSWORD` | Optional key password if the private key is encrypted | unset |
| `SPHINX_SERVER_DATA_DIR` | Root directory for DB, repos, builds, logs | `<project>/.sphinx_server` |
| `SPHINX_SERVER_DATABASE_URL` | Custom SQL database URL | `sqlite:///<data_dir>/sphinx_server.db` |
| `SPHINX_SERVER_ENV_MANAGER` | Default environment backend (`uv` or `pyenv`) when targets don’t override | `uv` |
| `SPHINX_SERVER_PYENV_DEFAULT_PYTHON_VERSION` | Python version passed to pyenv when repos lack `.python-version` | `3.11.8` |
| `SPHINX_SERVER_SECRET_KEY` | Secret key for session cookies | `change-me` |

### LDAP / LDAPS authentication

Set `SPHINX_SERVER_AUTH_BACKEND=ldap` to authenticate users against an external directory instead of the built-in password database. When LDAP is enabled:

- Users sign in with their directory credentials (SIMPLE bind) over LDAP or LDAPS.
- Profile details (full name + email) are synchronized on each login and new accounts are provisioned automatically with `SPHINX_SERVER_LDAP_DEFAULT_ROLE`.
- Account and user-management forms in the UI become read-only because modifications should happen inside the directory.
- Optionally set `SPHINX_SERVER_LDAP_ADMIN_GROUP_DN`, `SPHINX_SERVER_LDAP_CONTRIBUTOR_GROUP_DN`, and/or `SPHINX_SERVER_LDAP_VIEWER_GROUP_DN` to map group membership to Sphinx Server roles (the first match wins; defaults are used when no groups match). Use `SPHINX_SERVER_LDAP_GROUP_MEMBER_ATTRIBUTE` and `SPHINX_SERVER_LDAP_GROUP_MEMBER_VALUE_TEMPLATE` to match whatever attribute/value style your directory uses (for example `{username}` with `memberUid`).
- Authenticated LDAP users are tracked only in the browser session—no user rows are created in the local database.

The following environment variables customize the LDAP integration (see `.env` for commented examples):

| Variable | Description |
| --- | --- |
| `SPHINX_SERVER_LDAP_SERVER_URI` | LDAP/LDAPS URI, e.g. `ldaps://ldap.example.com:636` |
| `SPHINX_SERVER_LDAP_USE_SSL` / `SPHINX_SERVER_LDAP_VERIFY_SSL` | Enable LDAPS and control certificate validation |
| `SPHINX_SERVER_LDAP_CA_CERT_PATH` | Optional custom CA bundle when verifying certificates |
| `SPHINX_SERVER_LDAP_BIND_DN` / `SPHINX_SERVER_LDAP_BIND_PASSWORD` | Service account used to search for users (required when no DN template is supplied) |
| `SPHINX_SERVER_LDAP_USER_BASE_DN` | Base DN for user searches |
| `SPHINX_SERVER_LDAP_USER_FILTER` | LDAP filter to match usernames (supports `{username}` placeholder) |
| `SPHINX_SERVER_LDAP_USER_DN_TEMPLATE` | Alternative to search/bind: format string for a direct user DN (e.g. `uid={username},ou=people,dc=example,dc=com`) |
| `SPHINX_SERVER_LDAP_TIMEOUT` | Connect/search timeout in seconds |
| `SPHINX_SERVER_LDAP_DEFAULT_ROLE` | Role assigned to new LDAP users (`viewer`, `contributor`, or `administrator`) |
| `SPHINX_SERVER_LDAP_FULL_NAME_ATTRIBUTE` / `SPHINX_SERVER_LDAP_EMAIL_ATTRIBUTE` | Attribute names used to populate local profile metadata |
| `SPHINX_SERVER_LDAP_ADMIN_GROUP_DN` / `SPHINX_SERVER_LDAP_CONTRIBUTOR_GROUP_DN` / `SPHINX_SERVER_LDAP_VIEWER_GROUP_DN` | Optional LDAP group DNs whose membership overrides the user role |
| `SPHINX_SERVER_LDAP_GROUP_MEMBER_ATTRIBUTE` | LDAP attribute used for membership queries (e.g., `member`, `memberUid`, `uniqueMember`) |
| `SPHINX_SERVER_LDAP_GROUP_MEMBER_VALUE_TEMPLATE` | Format string inserted into the membership filter. Supports `{user_dn}` (default) and `{username}` |

All of these values can be edited manually or via **Admin → Settings**, which writes the updated values back to the `.env` file so they persist across restarts.

A default `admin` user (password: `password`) is provisioned automatically the first time the database is created. The account cannot access other pages until the password is updated.

Repository-specific secrets (e.g., GitHub PATs) can be stored per repo when you create it; tokens are injected into clone URLs and removed immediately after the initial clone.

## Folder layout
```
src/sphinx_server/
├── app.py            # FastAPI factory, middleware & router wiring
├── auth.py           # Password hashing, role guards, default-user seeding
├── build_service.py  # Git clone/fetch + Sphinx build executor and queue
├── config.py         # Pydantic settings + paths
├── database.py       # SQLModel engine/session helpers
├── git_utils.py      # Git command helpers (token-aware)
├── models.py         # SQLModel tables (repos, targets, builds, users)
└── web/              # Routers (docs, admin, auth/account) + templates
```

## Next steps
- Wire provider-specific webhooks (GitHub/GitLab) to automatically enqueue builds when tracked refs change.
- Support per-repo build command overrides (poetry, tox, etc.) and matrix builds per ref.
