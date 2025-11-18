User guide
==========

This guide walks through the main screens and configuration options in Sphinx Server so administrators and contributors know what each field does.

Getting started
---------------

1. Install and run the service

   .. code-block:: bash

      pip install -e .
      sphinx-server  # or python -m sphinx_server.main

   By default the app binds to ``http://127.0.0.1:8000``. Change host/port via the ``SPHINX_SERVER_HOST`` and ``SPHINX_SERVER_PORT`` variables (see :ref:`configuration`).

2. Sign in with the bootstrap admin credentials

   Visit ``/login`` and use ``admin`` / ``password``. You will be prompted to change the password on first login.

3. Explore the UI

   - **Docs explorer** (``/``): lists tracked repositories and their built artifacts.
   - **Admin → Repositories** (``/admin``): add/edit repositories, tracked refs, kick off builds, and view logs.
   - **Admin → Settings** (``/admin/settings``): edit environment variables that control the service.
   - **Admin → Users** (``/admin/users``): manage local users when database auth is active; read-only overview when LDAP is enabled.


Authentication modes
--------------------

Sphinx Server supports two backends, chosen by ``SPHINX_SERVER_AUTH_BACKEND``:

- ``database`` (default): users live in the local database. Admins can create, edit roles, and reset passwords in **Admin → Users**.
- ``ldap``: users authenticate against an external directory. Profile and password fields become read-only in the UI; group membership can map to Sphinx roles.

When LDAP is enabled, set at least ``SPHINX_SERVER_LDAP_SERVER_URI``, ``SPHINX_SERVER_LDAP_USER_BASE_DN`` (or ``SPHINX_SERVER_LDAP_USER_DN_TEMPLATE``), and ``SPHINX_SERVER_LDAP_BIND_DN`` / ``SPHINX_SERVER_LDAP_BIND_PASSWORD`` so the service account can look up users. To map LDAP groups to roles, provide one or more of:

- ``SPHINX_SERVER_LDAP_ADMIN_GROUP_DN`` → maps to administrator
- ``SPHINX_SERVER_LDAP_CONTRIBUTOR_GROUP_DN`` → maps to contributor
- ``SPHINX_SERVER_LDAP_VIEWER_GROUP_DN`` → maps to viewer

The first matching group wins. Customize how membership is read with ``SPHINX_SERVER_LDAP_GROUP_MEMBER_ATTRIBUTE`` (e.g. ``member``, ``memberUid``) and ``SPHINX_SERVER_LDAP_GROUP_MEMBER_VALUE_TEMPLATE`` (``{user_dn}`` or ``{username}``).


Account page
------------

- **Full name / Email**: editable only when using the database backend. LDAP users see these fields grayed out; updates must happen in the directory.
- **Username**: displayed for reference; not editable.
- **Change password**: available only when using the database backend. LDAP users must change passwords in the directory.


Repository management (Admin → Repositories)
--------------------------------------------

- **Name**: label shown in the UI.
- **Provider**: choose GitHub, GitLab, or Generic to tailor clone URLs and badges.
- **Repository URL**: any cloneable Git URL (HTTPS or SSH). Private HTTPS URLs may need an **Auth token**; SSH URLs rely on whatever SSH agent/keys are available on the host.
- **Docs path**: relative path inside the repo where Sphinx docs live (default ``docs``).
- **Public docs**: if enabled, built artifacts for this repo can be viewed without signing in.
- **Auth token**: optional personal access token for HTTPS clones of private repos; stored only for this repo.
- **Deploy key**: optional private SSH key dedicated to this repo.
- **Verify SSL**: control TLS verification when cloning over HTTPS.

Tracked targets (per repository)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

- **Ref type**: ``branch`` or ``tag``.
- **Ref name**: branch/tag to build.
- **Auto build**: when enabled, the auto-build monitor will re-trigger builds when the remote ref advances.
- **Environment manager**: override the global default (``uv`` or ``pyenv``) for this target if needed.


Builds and artifacts
--------------------

- **Build now**: enqueue an immediate build for the selected target.
- **Build logs**: live tail and historical logs are available per build under the repository page.
- **Artifacts**: the latest successful build for each target is served at ``/artifacts/<repo>/<target>/`` and linked from the docs explorer.


Settings (Admin → Settings)
---------------------------

These settings are persisted to ``.env`` when edited in the UI:

- **Host / Port**: network binding for the web server.
- **Reload**: enable uvicorn reload during development.
- **Data directory**: root folder for DB, repos, builds, logs, and virtualenvs.
- **Environment manager**: default build backend (``uv`` or ``pyenv``).
- **Default Python version**: used by pyenv when a repo does not specify one.
- **Git/Sphinx timeouts**: safety limits for long operations.
- **Build processes**: number of concurrent worker processes.
- **Auto-build interval**: seconds between polling cycles for refs with Auto build enabled.
- **Secret key**: session signing key; change for production.
- **HTTPS (SSL)**: set ``SPHINX_SERVER_SSL_CERTFILE`` and ``SPHINX_SERVER_SSL_KEYFILE`` (and ``SPHINX_SERVER_SSL_KEYFILE_PASSWORD`` if needed) to serve the UI over HTTPS.
- **LDAP settings**: all ``SPHINX_SERVER_LDAP_*`` options listed above are surfaced here when LDAP is active.

Complete settings file example
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Below is a representative ``.env`` you can copy and adjust. Values can also be written through **Admin → Settings**, which persists the file for you.

.. code-block:: ini

   # Core server
   SPHINX_SERVER_HOST=0.0.0.0
   SPHINX_SERVER_PORT=8000
   SPHINX_SERVER_RELOAD=false
   SPHINX_SERVER_SSL_CERTFILE=/path/to/cert.pem
   SPHINX_SERVER_SSL_KEYFILE=/path/to/key.pem
   # SPHINX_SERVER_SSL_KEYFILE_PASSWORD=optional
   SPHINX_SERVER_SECRET_KEY=change-me
   SPHINX_SERVER_DATA_DIR=/var/lib/sphinx-server

   # Build/runtime defaults
   SPHINX_SERVER_ENVIRONMENT_MANAGER=uv  # or pyenv
   SPHINX_SERVER_PYENV_DEFAULT_PYTHON_VERSION=3.11.8
   SPHINX_SERVER_GIT_DEFAULT_TIMEOUT=120
   SPHINX_SERVER_SPHINX_TIMEOUT=600
   SPHINX_SERVER_BUILD_PROCESSES=5
   SPHINX_SERVER_AUTO_BUILD_INTERVAL_SECONDS=60
   SPHINX_SERVER_DOCS_LINK_NEW_TAB=false

   # Authentication backend
   SPHINX_SERVER_AUTH_BACKEND=database  # or ldap

   # LDAP (only when AUTH_BACKEND=ldap)
   SPHINX_SERVER_LDAP_SERVER_URI=ldaps://ldap.example.com:636
   SPHINX_SERVER_LDAP_USE_SSL=true
   SPHINX_SERVER_LDAP_VERIFY_SSL=true
   # SPHINX_SERVER_LDAP_CA_CERT_PATH=/etc/ssl/certs/custom-ca.pem
   SPHINX_SERVER_LDAP_BIND_DN=cn=service,ou=accounts,dc=example,dc=com
   SPHINX_SERVER_LDAP_BIND_PASSWORD=secret
   SPHINX_SERVER_LDAP_USER_BASE_DN=ou=people,dc=example,dc=com
   # SPHINX_SERVER_LDAP_USER_FILTER=(uid={username})
   # SPHINX_SERVER_LDAP_USER_DN_TEMPLATE=uid={username},ou=people,dc=example,dc=com
   SPHINX_SERVER_LDAP_TIMEOUT=10
   SPHINX_SERVER_LDAP_DEFAULT_ROLE=viewer
   SPHINX_SERVER_LDAP_FULL_NAME_ATTRIBUTE=cn
   SPHINX_SERVER_LDAP_EMAIL_ATTRIBUTE=mail
   SPHINX_SERVER_LDAP_ADMIN_GROUP_DN=cn=admins,ou=groups,dc=example,dc=com
   SPHINX_SERVER_LDAP_CONTRIBUTOR_GROUP_DN=cn=contributors,ou=groups,dc=example,dc=com
   SPHINX_SERVER_LDAP_VIEWER_GROUP_DN=cn=viewers,ou=groups,dc=example,dc=com
   SPHINX_SERVER_LDAP_GROUP_MEMBER_ATTRIBUTE=member
   SPHINX_SERVER_LDAP_GROUP_MEMBER_VALUE_TEMPLATE={user_dn}


Command-line usage
------------------

Run the server directly:

.. code-block:: bash

   sphinx-server --help

Key environment variables (see also :ref:`configuration` in the README):

- ``SPHINX_SERVER_HOST`` / ``SPHINX_SERVER_PORT``: binding.
- ``SPHINX_SERVER_SSL_CERTFILE`` / ``SPHINX_SERVER_SSL_KEYFILE``: enable HTTPS.
- ``SPHINX_SERVER_AUTH_BACKEND``: ``database`` or ``ldap``.
- ``SPHINX_SERVER_ENV_MANAGER``: ``uv`` or ``pyenv`` for builds.
- ``SPHINX_SERVER_DATA_DIR``: storage root.


Troubleshooting
---------------

- **Login fails (database)**: check the username/password or reset via **Admin → Users**.
- **Login fails (LDAP)**: verify bind DN/password, server URI, base DN, and filters; inspect server logs for LDAP errors.
- **Build cannot clone repo**: confirm Auth token or Deploy key, and whether SSL verification should be disabled for the remote.
- **Docs not updating**: ensure Auto build is enabled or trigger **Build now**; check build logs for errors.
