# Sphinx Server

Sphinx Server is a lightweight control plane that keeps track of documentation repositories hosted on GitHub, GitLab, or any generic Git endpoint. It offers three cooperating pieces inside a single FastAPI deployment:

1. **Administrator UI** – browse to `/admin` to onboard repositories, choose the branches/tags to watch, and trigger builds on demand.
2. **Build worker** – a background queue clones/fetches repositories, checks out the selected refs, and runs `sphinx-build` to generate HTML artifacts.
3. **Documentation explorer** – the public landing page (`/`) lists every tracked repository and exposes the generated Sphinx sites grouped by branch/tag.

## Features
- Register multiple repositories with provider metadata, docs directories, and (optional) access tokens for private clones.
- Provide per-repository SSH deploy keys when HTTPS tokens aren’t available; keys stay scoped to each repo and are used only for its builds. During installs we also apply the repo’s optional `dev`/`docs` extras so build tooling/plugins are available just like in CI.
- Track any number of branches or tags per repository.
- Edit or delete repositories later and manage tracked branches/tags directly from the administrator UI.
- Kick off builds manually from the administrator UI; the worker logs git + Sphinx output per build and exposes the logs in the browser.
- Clean stale build artifacts/log files from the UI to keep storage tidy.
- Each tracked branch/tag always exposes its latest successful build at a stable URL, while the Sphinx UI gets an embedded selector (like Read the Docs) to hop between other tracked refs without reloading the admin site.
- Designate a "main" tracked target per repository, surface its pyproject metadata (name/version/summary) in the docs explorer, and update the metadata automatically whenever that target is rebuilt.
- Builds run in parallel inside isolated workspaces (fresh git clone + dedicated virtualenv per build), so multiple refs of the same repo can render simultaneously without interfering with each other.
- Build logs stream live in the admin UI, and each build records its duration so you can see how long docs took to compile.
- An auto-build monitor periodically checks refs marked with "Auto build" and automatically enqueues builds when remote commits advance.
- Build history indicates whether each run was triggered manually or automatically, so you can distinguish user-initiated rebuilds from watcher activity.
- Serve compiled documentation directly from the server under `/artifacts/<repo>/<target>/`.

## Requirements
- Python 3.10+
- Git CLI
- [uv](https://github.com/astral-sh/uv) CLI (used to create repo-specific virtual environments during builds)

## Usage
```bash
# Install dependencies in your environment
pip install -e .

# Run the FastAPI stack
sphinx-server  # or python -m sphinx_server.main
```

Browse to `http://localhost:8000/admin` to add repositories. After configuring at least one branch/tag target, click **Build now**; the worker clones the repo, provisions a dedicated uv-managed virtual environment for that repo (installing dependencies from its `pyproject.toml` or requirements files), and runs `sphinx-build`. Once the build completes successfully the documentation becomes available under `/docs/...` and `/artifacts/...`.

## Configuration
The service is configured through environment variables (prefix `SPHINX_SERVER_`). Useful settings:

| Variable | Description | Default |
| --- | --- | --- |
| `SPHINX_SERVER_HOST` | Bind host | `0.0.0.0` |
| `SPHINX_SERVER_PORT` | Bind port | `8000` |
| `SPHINX_SERVER_RELOAD` | Enable uvicorn reload (dev) | `false` |
| `SPHINX_SERVER_DATA_DIR` | Root directory for DB, repos, builds, logs | `<project>/.sphinx_server` |
| `SPHINX_SERVER_DATABASE_URL` | Custom SQL database URL | `sqlite:///<data_dir>/sphinx_server.db` |

Repository-specific secrets (e.g., GitHub PATs) can be stored per repo when you create it; tokens are injected into clone URLs and removed immediately after the initial clone.

## Folder layout
```
src/sphinx_server/
├── app.py            # FastAPI factory & router wiring
├── build_service.py  # Git clone/fetch + Sphinx build executor and queue
├── config.py         # Pydantic settings + paths
├── database.py       # SQLModel engine/session helpers
├── git_utils.py      # Git command helpers (token-aware)
├── models.py         # SQLModel tables for repositories, targets, builds
└── web/              # Routers + Jinja templates for admin/docs UIs
```

## Next steps
- Wire provider-specific webhooks (GitHub/GitLab) to automatically enqueue builds when tracked refs change.
- Add authentication/authorization around the admin surface.
- Support per-repo build command overrides (poetry, tox, etc.) and matrix builds per ref.
