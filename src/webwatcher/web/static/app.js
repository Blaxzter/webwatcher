/* webwatcher Web-UI - kein Framework, kein Build-Schritt.
 *
 * Das Formular wird aus dem Schema gebaut, das der Server liefert (FIELDS in
 * web/server.py). Eine neue Option in SITE_DEFAULTS braucht deshalb nur dort
 * einen Eintrag und erscheint hier von selbst. */

// Erlaubt den Betrieb unter einem Pfad-Präfix im Reverse Proxy.
const BASE = location.pathname.endsWith('/') ? location.pathname : location.pathname + '/';

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node[key] = value;
    else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child) node.append(child.nodeType ? child : document.createTextNode(child));
  }
  return node;
};

async function api(path, opts = {}) {
  const init = { headers: { 'Content-Type': 'application/json' }, method: opts.method || 'GET' };
  if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
  const res = await fetch(BASE + path, init);
  const data = (res.headers.get('content-type') || '').includes('json') ? await res.json() : null;
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

let toastTimer = null;
function toast(message, isError = false) {
  const node = $('#toast');
  node.textContent = message;
  node.className = 'toast' + (isError ? ' err' : '');
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, isError ? 7000 : 3000);
}

function banner(message) {
  const node = $('#editor-error');
  node.textContent = message || '';
  node.hidden = !message;
}

// ---------------------------------------------------------------- Zustand

let schema = null;
let sites = [];
let editing = null; // { key: string|null, original: object }
let controls = {};
let picker = null;
let pollTimer = null;

const fmtTime = (iso) => {
  if (!iso) return '–';
  const date = new Date(iso);
  const today = new Date().toDateString() === date.toDateString();
  return date.toLocaleString('de-DE', today
    ? { hour: '2-digit', minute: '2-digit' }
    : { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
};

// ------------------------------------------------------------------ Liste

async function loadState() {
  const state = await api('api/state');
  sites = state.sites;

  const telegram = $('#telegram');
  telegram.textContent = state.telegram.configured
    ? `Telegram: ${state.telegram.chats} Chat(s)`
    : 'Telegram fehlt';
  telegram.className = 'chip' + (state.telegram.configured ? '' : ' bad');
  telegram.onclick = testTelegram;
  telegram.style.cursor = 'pointer';

  $('#user').textContent = state.user || '';
  $('#user').hidden = !state.user;
  renderList();
}

function renderList() {
  const box = $('#sites');
  box.textContent = '';
  $('#empty').hidden = sites.length > 0;

  for (const site of sites) {
    const target = site.kind === 'hetzner_stock'
      ? `Hetzner ${String(site.mapping.server_type || '').toUpperCase()} · `
        + ([].concat(site.mapping.locations || []).join(', ') || 'alle Standorte')
      : site.url;
    const sub = site.valid
      ? `${target} · alle ${site.interval}${site.window ? ` · Fenster ${site.window}` : ''}` +
        ` · zuletzt ${fmtTime(site.last_checked_at)}`
      : site.url;

    const acts = el('div', { class: 'acts', style: 'position:relative' },
      el('button', { text: 'Prüfen', title: 'Jetzt einmal prüfen',
        onclick: (ev) => runCheck(site.key, ev.currentTarget) }),
      el('button', { text: 'Bearbeiten', onclick: () => openEditor(site.key) }),
      el('button', { class: 'ghost', text: '⋯', title: 'Mehr',
        onclick: (ev) => moreMenu(site, ev.currentTarget) }),
    );

    const info = el('div', { class: 'info' },
      el('div', { class: 'name', text: site.name }),
      el('div', { class: 'sub', text: sub }),
      site.error ? el('div', { class: 'err-text', text: site.error }) : null,
      site.last_error && site.failures
        ? el('div', { class: 'err-text', text: `${site.failures}× Fehler: ${site.last_error}` })
        : null,
    );

    box.append(el('div', {
      class: 'site' + (site.enabled ? '' : ' off') + (site.valid ? '' : ' broken'),
    }, el('span', { class: 'dot ' + (site.status || '') }), info, acts));
  }
}

function closeMenus() {
  for (const menu of document.querySelectorAll('.menu')) menu.remove();
  for (const card of document.querySelectorAll('.site.menu-open')) card.classList.remove('menu-open');
}

function moreMenu(site, anchor) {
  const open = anchor.nextElementSibling?.classList.contains('menu');
  closeMenus();
  if (open) return; // zweiter Klick schliesst wieder

  const index = sites.findIndex((entry) => entry.key === site.key);
  const run = (label, task) => async () => {
    closeMenus();
    try {
      await task();
      toast(label);
      await loadState();
    } catch (err) {
      toast(err.message, true);
    }
  };
  const path = `api/sites/${encodeURIComponent(site.key)}`;

  const move = (offset) => {
    const order = sites.map((entry) => entry.key);
    const [moved] = order.splice(index, 1);
    order.splice(index + offset, 0, moved);
    return api('api/sites/reorder', { method: 'POST', body: { keys: order } });
  };

  const items = [
    ['Jetzt prüfen, ohne zu melden', () => api(`${path}/check?notify=0`, { method: 'POST' })],
    [site.enabled ? 'Deaktivieren' : 'Aktivieren',
      () => api(path, { method: 'PUT', body: { ...site.mapping, enabled: !site.enabled } })],
    ['Baseline zurücksetzen', () => api(`${path}/reset`, { method: 'POST' })],
    index > 0 ? ['Nach oben', () => move(-1)] : null,
    index < sites.length - 1 ? ['Nach unten', () => move(1)] : null,
  ].filter(Boolean);

  const menu = el('div', { class: 'menu' },
    ...items.map(([label, task]) => el('button', { text: label, onclick: run(label, task) })),
    el('button', {
      class: 'danger', text: 'Löschen',
      onclick: () => {
        if (!confirm(`"${site.name}" mit Verlauf und Screenshots löschen?`)) return;
        run('Gelöscht.', () => api(path, { method: 'DELETE' }))();
      },
    }));

  anchor.after(menu);
  anchor.closest('.site')?.classList.add('menu-open');
}

async function runCheck(key, button) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'läuft …';
  try {
    const result = await api(`api/sites/${encodeURIComponent(key)}/check`, { method: 'POST' });
    toast(`${result.describe} (${result.duration_ms} ms)`, result.status === 'error');
    await loadState();
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

async function testTelegram() {
  try {
    const result = await api('api/telegram/test', { method: 'POST' });
    toast(result.ok ? `Testnachricht verschickt (@${result.bot}).` : result.error, !result.ok);
  } catch (err) {
    toast(err.message, true);
  }
}

// --------------------------------------------------------------- Editor

function openEditor(key) {
  const site = key ? sites.find((entry) => entry.key === key) : null;
  editing = { key, original: site ? { ...site.mapping } : {} };
  $('#editor-title').textContent = site ? site.name : 'Neue Seite';
  $('#editor').hidden = false;
  banner('');
  $('#editor-hint').textContent = site && site.updated_by ? `zuletzt von ${site.updated_by}` : '';
  buildForm();
  showTab('settings');
  stopPicker();
  clearInterval(pollTimer);
}

function closeEditor() {
  $('#editor').hidden = true;
  editing = null;
  controls = {};
  stopPicker();
  startPolling();
  loadState().catch((err) => toast(err.message, true));
}

function showTab(name) {
  for (const button of document.querySelectorAll('.tabs button')) {
    button.classList.toggle('on', button.dataset.tab === name);
  }
  for (const panel of document.querySelectorAll('.panel')) {
    panel.hidden = panel.dataset.panel !== name;
  }
  if (name === 'history') loadHistory();
  if (name === 'content') loadContent();
}

// -- Formular aus dem Schema ------------------------------------------------

function buildForm() {
  const form = $('#form');
  form.textContent = '';
  controls = {};

  form.append(el('div', { id: 'preview-out' }));

  const groups = [];
  for (const field of schema.fields) {
    let group = groups.find((entry) => entry.name === field.group);
    if (!group) groups.push((group = { name: field.group, fields: [] }));
    group.fields.push(field);
  }

  for (const group of groups) {
    const set = el('fieldset', {}, el('legend', { text: group.name }));
    for (const field of group.fields) {
      const control = makeControl(field, editing.original[field.name]);
      controls[field.name] = control;
      const wide = ['textarea', 'list', 'map', 'windows', 'multi'].includes(field.type);
      set.append(el('div', { class: 'field', 'data-field': field.name, 'data-only': field.only },
        el('label', { text: field.label + (field.required ? ' *' : '') }),
        el('div', { class: wide ? 'wide' : '' },
          control.el,
          field.help ? el('div', { class: 'help', text: field.help }) : null),
      ));
    }
    form.append(set);
  }
  controls.kind.el.addEventListener('change', applyKind);
  applyKind();
}

const currentKind = () => (controls.kind && controls.kind.read()) || schema.defaults.kind || 'page';

/* Felder mit `only` gelten nur für eine Art (page / hetzner_stock); die
 * anderen samt leer gewordener Gruppen ausblenden. */
function applyKind() {
  const kind = currentKind();
  for (const node of document.querySelectorAll('#form .field[data-only]')) {
    node.hidden = node.dataset.only !== kind;
  }
  for (const set of document.querySelectorAll('#form fieldset')) {
    set.hidden = !set.querySelector('.field:not([hidden])');
  }
  document.querySelector('.tabs button[data-tab="picker"]').hidden = kind !== 'page';
}

function placeholderFor(field) {
  const value = schema.defaults[field.name];
  if (value === null || value === undefined || value === '') return 'nicht gesetzt';
  if (Array.isArray(value)) return value.length ? value.join(', ') : 'nicht gesetzt';
  if (typeof value === 'object') return 'Standard';
  return `Standard: ${value}`;
}

function makeControl(field, value) {
  switch (field.type) {
    case 'bool': {
      const node = el('select', {},
        el('option', { value: '', text: `Standard (${schema.defaults[field.name] ? 'ja' : 'nein'})` }),
        el('option', { value: 'true', text: 'ja' }),
        el('option', { value: 'false', text: 'nein' }));
      node.value = value === undefined ? '' : String(Boolean(value));
      return { el: node, read: () => (node.value === '' ? undefined : node.value === 'true'),
               set: (next) => { node.value = String(Boolean(next)); } };
    }
    case 'enum': {
      const node = el('select', {},
        el('option', { value: '', text: `Standard (${schema.defaults[field.name]})` }),
        ...field.options.map((option) => el('option', { value: option, text: option })));
      node.value = value === undefined ? '' : String(value);
      return { el: node, read: () => node.value || undefined, set: (next) => { node.value = next; } };
    }
    case 'int': {
      const node = el('input', { type: 'number', min: '1', placeholder: placeholderFor(field) });
      if (value !== undefined) node.value = value;
      return { el: node, read: () => (node.value === '' ? undefined : Number(node.value)),
               set: (next) => { node.value = next; } };
    }
    case 'textarea': {
      const node = el('textarea', { rows: '4', placeholder: placeholderFor(field) });
      if (value !== undefined) node.value = value;
      return { el: node, read: () => node.value.trim() || '', set: (next) => { node.value = next; } };
    }
    case 'list': {
      const node = el('textarea', { rows: '3', placeholder: 'eine Angabe pro Zeile' });
      if (Array.isArray(value)) node.value = value.join('\n');
      else if (typeof value === 'string') node.value = value;
      return {
        el: node,
        read: () => node.value.split('\n').map((line) => line.trim()).filter(Boolean),
        set: (next) => { node.value = (next || []).join('\n'); },
        append: (item) => {
          const current = node.value.split('\n').map((line) => line.trim()).filter(Boolean);
          if (!current.includes(item)) current.push(item);
          node.value = current.join('\n');
        },
      };
    }
    case 'map': {
      const node = el('textarea', { rows: '3', placeholder: 'Name: Wert  (eine Zeile je Header)' });
      if (value && typeof value === 'object') {
        node.value = Object.entries(value).map(([key, val]) => `${key}: ${val}`).join('\n');
      }
      return {
        el: node,
        read: () => {
          const out = {};
          for (const line of node.value.split('\n')) {
            const at = line.indexOf(':');
            if (at < 1) continue;
            out[line.slice(0, at).trim()] = line.slice(at + 1).trim();
          }
          return out;
        },
        set: () => {},
      };
    }
    case 'multi': {
      const boxes = field.options.map((option) => {
        const input = el('input', { type: 'checkbox', value: option });
        if (Array.isArray(value) && value.includes(option)) input.checked = true;
        return el('label', { class: 'check' }, input, option);
      });
      const node = el('div', { class: 'chips' }, ...boxes);
      return {
        el: node,
        read: () => Array.from(node.querySelectorAll('input:checked')).map((input) => input.value),
        set: () => {},
      };
    }
    case 'viewport': {
      const width = el('input', { type: 'number', placeholder: schema.defaults.viewport?.width ?? 1440 });
      const height = el('input', { type: 'number', placeholder: schema.defaults.viewport?.height ?? 900 });
      if (value && typeof value === 'object') {
        if (value.width) width.value = value.width;
        if (value.height) height.value = value.height;
      }
      const node = el('div', { class: 'pair' }, width, el('span', { text: '×' }), height, el('span', { text: 'px' }));
      return {
        el: node,
        read: () => {
          if (!width.value && !height.value) return undefined;
          return {
            width: Number(width.value || schema.defaults.viewport?.width || 1440),
            height: Number(height.value || schema.defaults.viewport?.height || 900),
          };
        },
        set: () => {},
      };
    }
    case 'windows':
      return makeWindowsControl(value);
    default: {
      const node = el('input', { type: 'text', placeholder: placeholderFor(field) });
      if (value !== undefined) node.value = value;
      return { el: node, read: () => node.value.trim(), set: (next) => { node.value = next; } };
    }
  }
}

function makeWindowsControl(value) {
  const rows = el('div');
  const addRow = (window = {}) => {
    const from = el('input', { type: 'text', placeholder: 'von 08:00', value: window.from || '' });
    const to = el('input', { type: 'text', placeholder: 'bis 18:00', value: window.to || '' });
    const interval = el('input', { type: 'text', placeholder: 'Takt 2m', value: window.interval || '' });
    const days = el('input', {
      type: 'text', placeholder: 'Tage (mo,di – leer = täglich)',
      value: Array.isArray(window.days) ? window.days.join(',') : (window.days || ''),
    });
    const row = el('div', { class: 'window-row' }, from, to, interval, days,
      el('button', { class: 'ghost danger', text: '✕', type: 'button',
        onclick: () => row.remove() }));
    row._read = () => {
      if (![from, to, interval, days].some((input) => input.value.trim())) return null;
      const out = { from: from.value.trim(), to: to.value.trim(), interval: interval.value.trim() };
      const list = days.value.split(',').map((day) => day.trim()).filter(Boolean);
      if (list.length) out.days = list;
      return out;
    };
    rows.append(row);
  };

  (Array.isArray(value) ? value : []).forEach(addRow);
  const node = el('div', {}, rows,
    el('button', { class: 'ghost', type: 'button', text: '+ Zeitfenster',
      onclick: () => addRow() }));

  return {
    el: node,
    read: () => Array.from(rows.children).map((row) => row._read()).filter(Boolean),
    set: () => {},
  };
}

/* Leere Felder bedeuten "nicht gesetzt", damit die Defaults greifen. Eine leere
 * Liste bleibt nur erhalten, wenn sie vorher schon dastand - das ist die
 * einzige Art, einen globalen defaults-Wert bewusst abzuräumen. */
function readForm() {
  const mapping = {};
  const kind = currentKind();
  for (const [name, control] of Object.entries(controls)) {
    const only = schema.fields.find((field) => field.name === name)?.only;
    if (only && only !== kind) continue;
    const value = control.read();
    if (value === undefined || value === '') continue;
    const isEmpty = (Array.isArray(value) && !value.length)
      || (value && typeof value === 'object' && !Array.isArray(value) && !Object.keys(value).length);
    if (isEmpty && !(name in editing.original)) continue;
    mapping[name] = value;
  }
  if (editing.original.key) mapping.key = editing.original.key;
  return mapping;
}

async function save() {
  const mapping = readForm();
  if (currentKind() === 'page' && !mapping.url) { banner('Ohne URL geht es nicht.'); return; }
  if (currentKind() === 'hetzner_stock' && !mapping.server_type) {
    banner('Ohne Servertyp geht es nicht (z.B. cx53).');
    return;
  }
  const button = $('#save');
  button.disabled = true;
  try {
    if (editing.key) {
      await api(`api/sites/${encodeURIComponent(editing.key)}`, { method: 'PUT', body: mapping });
    } else {
      await api('api/sites', { method: 'POST', body: mapping });
    }
    toast('Gespeichert – gilt ab dem nächsten Durchlauf.');
    closeEditor();
  } catch (err) {
    banner(err.message);
  } finally {
    button.disabled = false;
  }
}

async function runPreview() {
  const button = $('#preview');
  button.disabled = true;
  showTab('settings');
  const out = $('#preview-out');
  out.textContent = '';
  out.append(el('div', { class: 'label', text: 'Vorschau' }),
    el('pre', { text: 'Wird zweimal gerendert – das dauert einen Moment …' }));
  try {
    const result = await api('api/preview', { method: 'POST', body: readForm() });
    out.textContent = '';
    if (!result.ok) {
      out.append(el('div', { class: 'label', text: 'Vorschau' }),
        el('pre', { class: 'err', text: `So könnte der Server das nicht prüfen:\n${result.error}` }));
      return;
    }
    const scope = result.matches > 1 ? `${result.matches} Elemente, ` : '';
    out.append(
      el('div', { class: 'label', text: 'Vorschau (headless, wie im Betrieb)' }),
      el('pre', { text: `${scope}${result.count} Zeilen, ${result.chars} Zeichen\n\n`
        + result.sample.join('\n') + (result.count > result.sample.length ? '\n…' : '') }));

    if (!result.unstable.length) {
      out.append(el('pre', { class: 'ok', text: 'Stabil: zwei Läufe ergaben denselben Inhalt.' }));
      return;
    }
    out.append(
      el('div', { class: 'label', text: 'Instabil – das gäbe Fehlalarme' }),
      el('pre', { class: 'warn', text: result.unstable.join('\n') }),
      el('button', {
        text: 'Als ignorierte Muster übernehmen',
        onclick: () => {
          for (const pattern of result.suggestions) controls.ignore_patterns.append(pattern);
          toast(`${result.suggestions.length} Muster übernommen.`);
          runPreview();
        },
      }));
  } catch (err) {
    out.textContent = '';
    out.append(el('pre', { class: 'err', text: err.message }));
  } finally {
    button.disabled = false;
  }
}

// -- Verlauf und Inhalt -----------------------------------------------------

async function loadHistory() {
  const box = $('#history');
  if (!editing.key) { box.textContent = ''; box.append(el('p', { class: 'empty', text: 'Noch nicht gespeichert.' })); return; }
  box.textContent = 'Lade …';
  try {
    const { checks } = await api(`api/sites/${encodeURIComponent(editing.key)}/history?limit=100`);
    box.textContent = '';
    if (!checks.length) { box.append(el('p', { class: 'empty', text: 'Noch keine Prüfung aufgezeichnet.' })); return; }
    const rows = checks.map((check) => el('tr', {},
      el('td', { text: fmtTime(check.checked_at) }),
      el('td', {}, el('span', { class: 'tag ' + check.status, text: check.status })),
      el('td', { text: check.status === 'changed' || check.status === 'minor'
        ? `+${check.added_lines} / −${check.removed_lines}` : '–' }),
      el('td', { text: check.duration_ms === null ? '–' : `${check.duration_ms} ms` }),
      el('td', { text: check.http_status || '–' }),
      el('td', { class: 'wrap', text: check.error || '' })));
    box.append(el('table', {},
      el('thead', {}, el('tr', {}, ...['Zeit', 'Status', 'Zeilen', 'Dauer', 'HTTP', 'Fehler']
        .map((title) => el('th', { text: title })))),
      el('tbody', {}, ...rows)));
  } catch (err) {
    box.textContent = err.message;
  }
}

async function loadContent() {
  const box = $('#content');
  if (!editing.key) { box.textContent = 'Noch nicht gespeichert.'; return; }
  box.textContent = 'Lade …';
  try {
    const { content } = await api(`api/sites/${encodeURIComponent(editing.key)}/content`);
    box.textContent = content || '(noch keine Baseline – einmal prüfen lassen)';
    const site = sites.find((entry) => entry.key === editing.key);
    const panel = box.parentElement;
    panel.querySelector('.shot')?.remove();
    if (site && site.has_screenshot) {
      panel.append(el('img', {
        class: 'shot', alt: 'Letzter Screenshot',
        src: `${BASE}api/sites/${encodeURIComponent(editing.key)}/screenshot?v=${Date.now()}`,
      }));
    }
  } catch (err) {
    box.textContent = err.message;
  }
}

// ------------------------------------------------------------------ Picker

function pickStatus(text) { $('#pick-status').textContent = text || ''; }

async function startPicker() {
  const mapping = readForm();
  if (!mapping.url) { banner('Für den Picker braucht es zuerst eine URL.'); showTab('settings'); return; }
  banner('');
  const button = $('#pick-start');
  button.disabled = true;
  pickStatus('Seite wird auf dem Server geladen …');
  try {
    const view = await api('api/picker', { method: 'POST', body: mapping });
    picker = { id: view.session, mode: 'watch', broad: false };
    applyView(view);
    $('#pick-stage').hidden = false;
    $('#pick-hint').hidden = true;
    $('#pick-reload').hidden = false;
    $('#pick-stop').hidden = false;
    button.hidden = true;
    pickStatus('Ins Bild klicken, um einen Bereich zu wählen.');
  } catch (err) {
    pickStatus('');
    banner(err.message);
  } finally {
    button.disabled = false;
  }
}

function applyView(view) {
  picker.view = view;
  $('#pick-img').src = `${BASE}api/picker/${picker.id}/screenshot?v=${view.shot_version}`;
  const slider = $('#pick-scroll');
  slider.max = Math.max(0, view.page_height - view.viewport.height);
  slider.value = view.scroll_y;
  slider.hidden = slider.max === '0' || Number(slider.max) === 0;
  $('#pick-overlay').textContent = '';
}

async function stopPicker() {
  const current = picker;
  picker = null;
  $('#pick-stage').hidden = true;
  $('#pick-hint').hidden = false;
  $('#pick-current').hidden = true;
  $('#pick-reload').hidden = true;
  $('#pick-stop').hidden = true;
  $('#pick-start').hidden = false;
  pickStatus('');
  if (current) {
    try { await api(`api/picker/${current.id}`, { method: 'DELETE' }); } catch { /* egal */ }
  }
}

function drawRects(rects, ignoring) {
  const overlay = $('#pick-overlay');
  const img = $('#pick-img');
  overlay.textContent = '';
  if (!img.naturalWidth) return;
  const scale = img.clientWidth / img.naturalWidth;
  rects.forEach((rect, index) => {
    overlay.append(el('div', {
      class: 'hl' + (index ? ' extra' : '') + (ignoring ? ' ignore' : ''),
      style: `top:${rect.top * scale}px;left:${rect.left * scale}px;`
        + `width:${rect.width * scale}px;height:${rect.height * scale}px`,
    }));
  });
}

function showPicked(result) {
  const box = $('#pick-current');
  box.textContent = '';
  box.hidden = false;
  const ignoring = picker.mode === 'ignore';
  box.append(
    el('code', { class: result.fragile ? 'warn' : '', text: (result.fragile ? '⚠ ' : '') + result.selector }),
    el('span', { class: 'muted', text: result.matches === 1 ? '1 Treffer' : `${result.matches} Treffer` }),
    el('div', { class: 'grow' }),
    el('button', {
      class: 'primary',
      text: ignoring ? 'Ignorieren' : 'Als Bereich übernehmen',
      onclick: () => {
        if (ignoring) controls.ignore_selectors.append(result.selector);
        else controls.selector.set(result.selector);
        toast(ignoring ? 'Zu den ignorierten Elementen gelegt.' : 'Als beobachteter Bereich gesetzt.');
      },
    }));
  if (result.fragile) {
    box.append(el('div', { class: 'help', text:
      'Positionsbasiert (:nth-child) – bricht, sobald sich die Seite umbaut. '
      + 'Lieber ein Elternelement wählen.' }));
  }
}

async function onStageClick(event) {
  if (!picker) return;
  const img = $('#pick-img');
  const rect = img.getBoundingClientRect();
  const scale = img.naturalWidth / rect.width;
  const x = (event.clientX - rect.left) * scale;
  const y = (event.clientY - rect.top) * scale;
  try {
    const result = await api(`api/picker/${picker.id}/pick`, {
      method: 'POST', body: { x, y, broad: picker.broad },
    });
    drawRects(result.rects, picker.mode === 'ignore');
    showPicked(result);
  } catch (err) {
    pickStatus(err.message);
  }
}

let scrollTimer = null;
function onScroll(event) {
  const y = Number(event.target.value);
  clearTimeout(scrollTimer);
  scrollTimer = setTimeout(async () => {
    if (!picker) return;
    try {
      applyView(await api(`api/picker/${picker.id}/scroll`, { method: 'POST', body: { y } }));
    } catch (err) {
      pickStatus(err.message);
    }
  }, 180);
}

function setPickMode(mode) {
  picker && (picker.mode = mode);
  $('#pick-watch').className = mode === 'watch' ? 'on' : '';
  $('#pick-ignore').className = mode === 'ignore' ? 'on ignore' : '';
}

// -------------------------------------------------------------------- Start

function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (!editing) loadState().catch(() => {});
  }, 10000);
}

function wire() {
  $('#add').onclick = () => openEditor(null);
  $('#editor-close').onclick = closeEditor;
  $('#editor-cancel').onclick = closeEditor;
  $('#save').onclick = save;
  $('#preview').onclick = runPreview;
  $('#content-reload').onclick = loadContent;

  for (const button of document.querySelectorAll('.tabs button')) {
    button.onclick = () => showTab(button.dataset.tab);
  }

  $('#pick-start').onclick = startPicker;
  $('#pick-stop').onclick = stopPicker;
  $('#pick-reload').onclick = async () => {
    if (!picker) return;
    pickStatus('Lädt neu …');
    try { applyView(await api(`api/picker/${picker.id}/reload`, { method: 'POST' })); pickStatus(''); }
    catch (err) { pickStatus(err.message); }
  };
  $('#pick-watch').onclick = () => setPickMode('watch');
  $('#pick-ignore').onclick = () => setPickMode('ignore');
  $('#pick-broad').onchange = (event) => { if (picker) picker.broad = event.target.checked; };
  $('#pick-img').onclick = onStageClick;
  $('#pick-scroll').oninput = onScroll;

  document.addEventListener('click', (event) => {
    if (!event.target.closest('.acts')) closeMenus();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (document.querySelector('.menu')) closeMenus();
    else if (editing) closeEditor();
  });
  window.addEventListener('beforeunload', () => {
    if (picker) navigator.sendBeacon?.(`${BASE}api/picker/${picker.id}`);
  });
}

(async () => {
  wire();
  try {
    schema = await api('api/schema');
    await loadState();
    startPolling();
  } catch (err) {
    document.body.prepend(el('div', { class: 'banner err', text: `Start fehlgeschlagen: ${err.message}` }));
  }
})();
