(function () {
  if (window.__SPHINX_SERVER_NAV_LOADED) return;
  window.__SPHINX_SERVER_NAV_LOADED = true;
  const repoId = window.__SPHINX_SERVER_REPO;
  if (!repoId) return;

  const currentSlug = window.__SPHINX_SERVER_TARGET;
  const currentRef = window.__SPHINX_SERVER_REF_NAME || '';
  const currentType = (window.__SPHINX_SERVER_REF_TYPE || 'branch').toLowerCase();
  const currentVersion = window.__SPHINX_SERVER_VERSION || 'unknown';

  injectStyles();
  const headerContainer = injectHeaderMeta();

  function injectStyles() {
    const style = document.createElement('style');
    style.textContent = `
      .sphinx-server-meta { margin-top:0.5rem; font-size:0.85rem; color:#fff; display:flex; align-items:center; gap:0.5rem; }
      .sphinx-server-meta .ref-label { font-size:0.7rem; min-width:auto; padding:0.25rem 0.6rem; }
      .sphinx-server-selector { margin-top:0.6rem; }
      .sphinx-server-selector select { width:100%; padding:0.35rem 0.4rem; border-radius:6px; border:1px solid rgba(255,255,255,0.2); background:rgba(255,255,255,0.1); color:#fff; }
      .sphinx-server-selector select option { color:#000; }
      .sphinx-server-link-list { margin:2rem 1rem 1rem 1rem; padding:1rem 0 0 0; border-top:1px solid rgba(255,255,255,0.15); font-size:0.85rem; color:#fff; }
      .sphinx-server-link-list ul { list-style:none; margin:0; padding:0; }
      .sphinx-server-link-list li { margin-bottom:0.35rem; }
      .sphinx-server-link-list a { color:#9bdaf1; text-decoration:none; display:block; }
      .sphinx-server-link-list a.current { text-decoration:underline; font-weight:600; }
      .sphinx-server-version { opacity:0.8; }
      .ref-label { display:inline-flex; align-items:center; justify-content:center; padding:0.35rem 0.75rem; border-radius:999px; font-size:0.72rem; text-transform:uppercase; letter-spacing:0.08em; background:#dfe6e9; color:#2d3436; }
      .ref-label.branch { background:#c8e6c9; color:#1b5e20; }
      .ref-label.tag { background:#ffe0b2; color:#e65100; }
    `;
    document.head.appendChild(style);
  }

  function injectHeaderMeta() {
    const labelText = currentType === 'branch' ? 'Branch' : 'Tag';
    const meta = document.createElement('div');
    meta.className = 'sphinx-server-meta';
    meta.innerHTML = `
      <span class="ref-label ${currentType}">${labelText}: ${currentRef}</span>
      <span class="sphinx-server-version">v${currentVersion}</span>
    `;
    const container = document.querySelector('.wy-nav-side .wy-side-nav-search') ||
      document.querySelector('.wy-nav-side') ||
      document.querySelector('nav');
    if (container) {
      container.appendChild(meta);
    }
    return container;
  }

  function attachSelector(targets) {
    if (!headerContainer) return;
    const wrapper = document.createElement('div');
    wrapper.className = 'sphinx-server-selector';
    const select = document.createElement('select');
    select.style.width = '100%';
    targets.forEach((target) => {
      const option = document.createElement('option');
      option.value = target.url;
      const label = `${target.ref_type}: ${target.ref_name}`;
      option.textContent = label;
      if (target.slug === currentSlug) {
        option.selected = true;
      }
      select.appendChild(option);
    });
    select.addEventListener('change', (event) => {
      const value = event.target.value;
      if (value) {
        window.location.href = value;
      }
    });
    wrapper.appendChild(select);
    headerContainer.appendChild(wrapper);
  }

  function createContainer(targets) {
    const container = document.createElement('div');
    container.className = 'sphinx-server-link-list';

    const title = document.createElement('div');
    title.textContent = 'Tracked documentation';
    title.style.fontWeight = '600';
    title.style.marginBottom = '0.5rem';
    container.appendChild(title);

    const list = document.createElement('ul');
    targets.forEach((target) => {
      const li = document.createElement('li');
      const link = document.createElement('a');
      link.href = target.url;
      link.textContent = `${target.ref_type}: ${target.ref_name}`;
      if (target.slug === currentSlug) {
        link.classList.add('current');
      }
      li.appendChild(link);
      list.appendChild(li);
    });
    container.appendChild(list);
    return container;
  }

  function attach(container) {
    const nav = document.querySelector('.wy-nav-side .wy-side-scroll') ||
      document.querySelector('.wy-nav-side') ||
      document.querySelector('nav');
    if (!nav) {
      document.body.appendChild(container);
      return;
    }
    nav.appendChild(container);
  }

  fetch(`/docs/${repoId}/refs.json`)
    .then((resp) => resp.json())
    .then((data) => {
      if (!data || !Array.isArray(data.targets) || !data.targets.length) {
        return;
      }
      const available = data.targets.filter((t) => t.has_artifact && t.url);
      if (!available.length) {
        return;
      }
      attachSelector(available);
      const container = createContainer(available);
      attach(container);
    })
    .catch(() => {});
})();
