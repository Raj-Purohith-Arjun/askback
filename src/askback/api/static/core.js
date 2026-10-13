/* Shared helpers: DOM building, the API client, formatting and toasts.
 *
 * Loaded first; panels.js and app.js depend on everything exported here onto
 * `window`. Plain ES2020, no modules, no build step - the file is served as-is
 * from the package, including inside a container with no network.
 */

/* ─────────────────────────────────────────────────────────── DOM building */

/**
 * Build an element.
 *
 *   el('div', { class: 'card' }, el('span', {}, 'hi'))
 *
 * Children that are strings become text nodes, never markup. Every value shown
 * in this dashboard comes from the corpus, a model reply or a test case - i.e.
 * data we do not control - so there is deliberately no innerHTML path here.
 */
function el(tag, props, ...children) {
  const node = document.createElement(tag);

  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'style') Object.assign(node.style, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key in node && key !== 'list') {
      node[key] = value;
    } else {
      node.setAttribute(key, value);
    }
  }

  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** Replace an element's children in one shot. */
function mount(target, ...children) {
  const node = typeof target === 'string' ? document.getElementById(target) : target;
  node.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

const $ = (id) => document.getElementById(id);

/* ────────────────────────────────────────────────────────────── API client */

/**
 * Every call goes through here so error handling is uniform: FastAPI reports
 * problems as `{detail: ...}`, and the caller should see that message rather
 * than a bare "500".
 */
async function request(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers['content-type'] = 'application/json';
    options.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(path, options);
  } catch (cause) {
    throw new Error(`Cannot reach the server. Is it still running? (${cause.message})`);
  }

  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = text;
  }

  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : payload || response.statusText;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return payload;
}

const api = {
  get: (path, params) => {
    const query = params ? `?${new URLSearchParams(params)}` : '';
    return request('GET', path + query);
  },
  post: (path, body) => request('POST', path, body),
};

/* ──────────────────────────────────────────────────────────── formatting */

const fmt = {
  pct: (value) => `${Math.round((value || 0) * 100)}%`,
  score: (value) => (value === null || value === undefined ? '–' : Number(value).toFixed(2)),
  ms: (value) => `${Math.round(value || 0)} ms`,

  /** Report timestamps are ISO strings; show them in the reader's locale. */
  when: (iso) => {
    if (!iso) return '';
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
  },

  truncate: (text, limit) => {
    const value = String(text ?? '');
    return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
  },
};

/* ─────────────────────────────────────────────────────────────── feedback */

/** Transient message, bottom right. `kind` is 'info' | 'success' | 'error'. */
function toast(message, kind = 'info') {
  const node = el('div', { class: `toast ${kind}` }, message);
  $('toasts').append(node);
  // Errors linger: they usually need reading and often need acting on.
  setTimeout(() => node.remove(), kind === 'error' ? 7000 : 3200);
}

/** Wrap an async handler so a rejected promise surfaces instead of vanishing. */
function guard(fn) {
  return async (...args) => {
    try {
      return await fn(...args);
    } catch (error) {
      toast(error.message, 'error');
      console.error(error);
      return undefined;
    }
  };
}

/** Disable a button while its action runs, so it cannot be double-fired. */
async function withBusy(button, label, action) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = label;
  try {
    return await action();
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

/* ────────────────────────────────────────────────── shared view fragments */

/** A labelled statistic. Used for both the inspector and the eval summary. */
function statCard(label, value, tone = '', className = 'card') {
  return el('div', { class: className },
    el('div', { class: 'k' }, label),
    el('div', { class: `v ${tone}` }, value));
}

/** A horizontal score bar with a label and a numeric readout. */
function scoreBar(label, value, { max = 1, winner = false, dim = false } = {}) {
  const ratio = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  return el('div', {},
    el('div', { class: `bar-row ${winner ? 'winner' : ''}` },
      el('span', { class: 'bar-label', title: label }, label),
      el('span', { class: 'bar-value' }, fmt.score(value))),
    el('div', { class: 'bar-track' },
      el('div', { class: `bar-fill ${dim ? 'dim' : ''}`, style: { width: `${ratio * 100}%` } })));
}

/** A key/value line for the inspector. */
function kvRow(key, value) {
  return el('div', { class: 'kv' },
    el('span', { class: 'k' }, key),
    el('span', { class: 'v' }, value));
}

/** Build a table from a header list and an array of cell arrays. */
function dataTable(headers, rows, { numeric = [] } = {}) {
  return el('table', {},
    el('thead', {}, el('tr', {}, headers.map((h, i) =>
      el('th', { class: numeric.includes(i) ? 'num' : '' }, h)))),
    el('tbody', {}, rows.map((row) => el('tr', {}, row.map((cell, i) =>
      el('td', { class: numeric.includes(i) ? 'num' : '' },
        cell instanceof Node ? cell : String(cell ?? '')))))));
}

function emptyState(title, body) {
  return el('div', { class: 'empty-state' }, el('h3', {}, title), el('p', {}, body));
}

function passPill(passed, label) {
  return el('span', { class: `pill ${passed ? 'pass' : 'fail'}` }, label || (passed ? 'pass' : 'fail'));
}

/* Exported onto window for panels.js and app.js. */
Object.assign(window, {
  el, mount, $, api, fmt, toast, guard, withBusy,
  statCard, scoreBar, kvRow, dataTable, emptyState, passPill,
});
