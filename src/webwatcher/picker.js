/* webwatcher element picker - injected into the page by `webwatcher pick`.
 * Talks to Python through the bindings exposed by picker.py (__wwPick, ...). */
(() => {
  if (window.top !== window) return; // main frame only
  if (window.__wwPickerLoaded) return;
  window.__wwPickerLoaded = true;

  // ---------------------------------------------------------------- selectors

  // Reject generated names: css-1a2b3c, jsx-1234567, item_98765, ...
  const UNSTABLE_TOKEN = /(^|[-_])(\d{3,}|[0-9a-f]{6,})([-_]|$)/i;
  const UNSTABLE_CLASS = /^(is-|has-|js-|ng-|_)|(active|hover|focus|selected|open|show|hidden|current)$/i;

  const isUnique = (selector) => {
    try {
      return document.querySelectorAll(selector).length === 1;
    } catch {
      return false;
    }
  };

  const stableClasses = (el) =>
    Array.from(el.classList)
      .filter((c) => c.length > 1 && !UNSTABLE_TOKEN.test(c) && !UNSTABLE_CLASS.test(c))
      .slice(0, 3);

  /* Best selector for one element, ignoring its ancestors. */
  function partFor(el) {
    const tag = el.tagName.toLowerCase();

    if (el.id && !UNSTABLE_TOKEN.test(el.id)) {
      const sel = '#' + CSS.escape(el.id);
      if (isUnique(sel)) return { sel, unique: true };
    }

    for (const attr of ['data-testid', 'data-test-id', 'data-qa', 'data-test', 'itemprop', 'name']) {
      const value = el.getAttribute(attr);
      if (value && !UNSTABLE_TOKEN.test(value)) {
        const sel = `${tag}[${attr}="${value.replace(/["\\]/g, '\\$&')}"]`;
        if (isUnique(sel)) return { sel, unique: true };
      }
    }

    const sel = tag + stableClasses(el).map((c) => '.' + CSS.escape(c)).join('');
    return { sel, unique: isUnique(sel) };
  }

  /* Walk up until the accumulated path is unique in the document. */
  function buildSelector(el) {
    const direct = partFor(el);
    if (direct.unique) return direct.sel;

    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
      let part = partFor(node).sel;
      const parent = node.parentElement;
      if (parent) {
        let siblings = 0;
        for (const child of parent.children) {
          try {
            if (child.matches(part)) siblings++;
          } catch {
            /* ignore */
          }
        }
        if (siblings > 1) {
          part += `:nth-child(${Array.prototype.indexOf.call(parent.children, node) + 1})`;
        }
      }
      parts.unshift(part);
      const candidate = parts.join(' > ');
      if (isUnique(candidate)) return candidate;
      if (!parent || parent === document.body) break;
      node = parent;
    }
    return parts.join(' > ');
  }

  const countFor = (selector) => {
    try {
      return document.querySelectorAll(selector).length;
    } catch {
      return 0;
    }
  };

  /* Purely class based, ignoring id/data attributes and position, so it
   * deliberately matches every sibling of the same kind (list items, cards, ...). */
  function broadSelector(el) {
    const classes = stableClasses(el);
    if (!classes.length) return null;
    const sel = el.tagName.toLowerCase() + classes.map((c) => '.' + CSS.escape(c)).join('');
    return countFor(sel) ? sel : null;
  }

  // Exposed for `webwatcher pick --headless` tests and for debugging in DevTools.
  window.__wwBuildSelector = buildSelector;
  window.__wwBroadSelector = broadSelector;

  // ---------------------------------------------------------------------- ui

  const CSS_TEXT = `
    :host { all: initial; }
    .hl {
      position: fixed; pointer-events: none; z-index: 2147483646;
      border: 2px solid #2f81f7; background: rgba(47,129,247,.14); border-radius: 2px;
    }
    .hl.ignore { border-color: #e5534b; background: rgba(229,83,75,.16); }
    /* further matches of a class selector */
    .hl.extra { border-style: dashed; background: rgba(47,129,247,.07); }
    .hl.extra.ignore { background: rgba(229,83,75,.07); }
    #tag {
      position: fixed; pointer-events: none; z-index: 2147483647;
      background: #2f81f7; color: #fff; font: 11px/1.5 ui-monospace, Menlo, Consolas, monospace;
      padding: 1px 6px; border-radius: 3px; max-width: 60vw; overflow: hidden;
      text-overflow: ellipsis; white-space: nowrap;
    }
    #tag.ignore { background: #e5534b; }
    #tag.fragile { background: #9e6a03; }
    #panel {
      position: fixed; top: 12px; right: 12px; width: 340px; z-index: 2147483647;
      background: #0d1117; color: #e6edf3; border: 1px solid #30363d; border-radius: 8px;
      font: 12px/1.45 -apple-system, "Segoe UI", Roboto, sans-serif;
      box-shadow: 0 8px 28px rgba(0,0,0,.5); max-height: 92vh; display: flex; flex-direction: column;
    }
    #head { padding: 9px 12px; border-bottom: 1px solid #30363d; font-weight: 600; display: flex;
            justify-content: space-between; align-items: center; }
    #head small { font-weight: 400; color: #7d8590; }
    .body { padding: 10px 12px; overflow-y: auto; }
    .row { display: flex; gap: 6px; margin-bottom: 8px; }
    button {
      font: inherit; padding: 5px 9px; border-radius: 6px; cursor: pointer;
      background: #21262d; color: #e6edf3; border: 1px solid #30363d;
    }
    button:hover { background: #30363d; }
    button.on { background: #2f81f7; border-color: #2f81f7; color: #fff; }
    button.on.ignore { background: #e5534b; border-color: #e5534b; }
    button.primary { background: #238636; border-color: #238636; color: #fff; }
    button.paused { background: #9e6a03; border-color: #9e6a03; color: #fff; }
    .label { color: #7d8590; text-transform: uppercase; font-size: 10px;
             letter-spacing: .04em; margin: 10px 0 4px; }
    .item { display: flex; justify-content: space-between; align-items: center; gap: 6px;
            background: #161b22; border: 1px solid #30363d; border-radius: 5px;
            padding: 3px 6px; margin-bottom: 3px; }
    .item code { font: 11px ui-monospace, Menlo, Consolas, monospace; word-break: break-all; }
    .item button { padding: 0 6px; line-height: 1.4; border: 0; background: transparent; color: #7d8590; }
    .item button:hover { color: #e5534b; background: transparent; }
    .empty { color: #7d8590; font-style: italic; }
    pre { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 8px;
          margin: 0; font: 11px/1.45 ui-monospace, Menlo, Consolas, monospace;
          white-space: pre-wrap; word-break: break-word; max-height: 240px; overflow-y: auto; }
    .ok { color: #3fb950; }
    .warn { color: #d29922; }
    .err { color: #f85149; }
    .hint { color: #7d8590; margin-top: 8px; }
  `;

  const PANEL_HTML = `
    <div id="head"><span>webwatcher</span><small id="mode-hint">Esc = abbrechen</small></div>
    <div class="body">
      <div class="row">
        <button id="m-watch" class="on" style="flex:1">Beobachten</button>
        <button id="m-ignore" style="flex:1">Ignorieren</button>
      </div>
      <div class="row">
        <button id="pause" style="flex:1">Seite bedienen</button>
      </div>
      <div id="state"></div>
      <div class="row" style="margin-top:10px">
        <button id="preview" style="flex:1">Vorschau</button>
        <button id="finish" class="primary" style="flex:1">Fertig</button>
      </div>
      <div id="out"></div>
      <div class="hint">
        Klick = auswählen · ↑↓ = Eltern-/Kindelement ·
        Tab = alle gleichartigen (Klassen) · Esc = abbrechen
      </div>
    </div>
  `;

  let mode = 'watch';
  let paused = false;
  let broad = false; // Tab: match every element of the same kind, not just this one
  let hovered = null; // element under the cursor
  let depth = 0; // how many levels up from `hovered` we walked with the arrow keys
  let host, shadow, boxes, tag, panel, stateEl, outEl;

  /* Clicking always hits the innermost element, so ArrowUp walks to the parent. */
  function currentTarget() {
    let el = hovered;
    if (!el) return null;
    for (let i = 0; i < depth; i++) {
      const parent = el.parentElement;
      if (!parent || parent === document.body || parent === document.documentElement) break;
      el = parent;
    }
    return el;
  }

  const esc = (s) =>
    String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

  function build() {
    host = document.createElement('div');
    host.id = '__ww_picker_host';
    host.setAttribute('data-ww-picker', '');
    shadow = host.attachShadow({ mode: 'open' });

    const style = document.createElement('style');
    style.textContent = CSS_TEXT;
    boxes = document.createElement('div');
    boxes.id = 'boxes';
    tag = document.createElement('div');
    tag.id = 'tag';
    tag.style.display = 'none';
    panel = document.createElement('div');
    panel.id = 'panel';
    panel.innerHTML = PANEL_HTML;

    shadow.append(style, boxes, tag, panel);
    document.documentElement.appendChild(host);

    stateEl = shadow.getElementById('state');
    outEl = shadow.getElementById('out');

    shadow.getElementById('m-watch').onclick = () => setMode('watch');
    shadow.getElementById('m-ignore').onclick = () => setMode('ignore');
    shadow.getElementById('pause').onclick = togglePause;
    shadow.getElementById('preview').onclick = doPreview;
    shadow.getElementById('finish').onclick = () => window.__wwFinish();
  }

  function setMode(next) {
    mode = next;
    const watch = shadow.getElementById('m-watch');
    const ignore = shadow.getElementById('m-ignore');
    watch.className = next === 'watch' ? 'on' : '';
    ignore.className = next === 'ignore' ? 'on ignore' : '';
  }

  function togglePause() {
    paused = !paused;
    const btn = shadow.getElementById('pause');
    btn.textContent = paused ? '▶ Weiter auswählen' : 'Seite bedienen';
    btn.className = paused ? 'paused' : '';
    if (paused) {
      shadow.getElementById('mode-hint').textContent = 'Picker pausiert';
      hideHighlight();
    } else {
      updateBroadHint();
    }
  }

  function hideHighlight() {
    boxes.innerHTML = '';
    tag.style.display = 'none';
  }

  /* The selector a click would store for this element. */
  function selectorFor(el) {
    if (broad) {
      const wide = broadSelector(el);
      if (wide) return wide;
    }
    return buildSelector(el);
  }

  function drawBox(rect, cls) {
    const div = document.createElement('div');
    div.className = 'hl ' + cls;
    div.style.cssText = `top:${rect.top}px;left:${rect.left}px;width:${rect.width}px;height:${rect.height}px;`;
    boxes.appendChild(div);
  }

  function highlight(el) {
    const rect = el.getBoundingClientRect();
    if (!rect.width && !rect.height) return hideHighlight();

    const ignoring = mode === 'ignore';
    const selector = selectorFor(el);
    const matches = countFor(selector);
    // Positional selectors break as soon as the page structure shifts.
    const fragile = selector.includes(':nth-child');

    boxes.innerHTML = '';
    if (matches > 1) {
      for (const other of document.querySelectorAll(selector)) {
        if (other === el) continue;
        const r = other.getBoundingClientRect();
        if (r.width || r.height) drawBox(r, ignoring ? 'extra ignore' : 'extra');
      }
    }
    drawBox(rect, ignoring ? 'ignore' : '');

    tag.className = ignoring ? 'ignore' : fragile ? 'fragile' : '';
    tag.textContent =
      (fragile ? '⚠ ' : '') + selector + (matches > 1 ? `  ×${matches}` : '');
    tag.style.display = 'block';
    tag.style.top = (rect.top > 22 ? rect.top - 20 : rect.bottom + 4) + 'px';
    tag.style.left = Math.max(2, rect.left) + 'px';
  }

  const fromPicker = (event) => {
    const target = event.target;
    return target === host || (host && host.contains(target));
  };

  function onOver(event) {
    if (paused || fromPicker(event)) return;
    const el = event.target;
    if (!el || el.nodeType !== 1 || el === document.body || el === document.documentElement) return;
    hovered = el;
    depth = 0;
    highlight(el);
  }

  function onClick(event) {
    if (paused || fromPicker(event)) return;
    const el = currentTarget() || event.target;
    if (!el || el.nodeType !== 1) return;
    event.preventDefault();
    event.stopPropagation();
    window.__wwPick(mode, selectorFor(el)).then(render);
  }

  function onKey(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      window.__wwCancel();
      return;
    }
    if (paused || !hovered) return;
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      event.preventDefault();
      depth = event.key === 'ArrowUp' ? depth + 1 : Math.max(0, depth - 1);
      const target = currentTarget();
      if (target) highlight(target);
    } else if (event.key === 'Tab') {
      event.preventDefault();
      const target = currentTarget();
      // Only offer the broad mode when the element actually has usable classes.
      if (!broad && target && !broadSelector(target)) {
        flashHint('Kein brauchbarer Klassenname an diesem Element');
        return;
      }
      broad = !broad;
      updateBroadHint();
      if (target) highlight(target);
    }
  }

  function updateBroadHint() {
    shadow.getElementById('mode-hint').textContent = broad
      ? 'Tab: alle gleichartigen'
      : 'Tab: nur dieses';
  }

  function flashHint(text) {
    const hint = shadow.getElementById('mode-hint');
    hint.textContent = text;
    setTimeout(updateBroadHint, 1800);
  }

  // ------------------------------------------------------------- rendering

  function itemsHtml(list, kind) {
    if (!list.length) return '<div class="empty">–</div>';
    return list
      .map((value) => {
        const fragile = String(value).includes(':nth-child');
        return (
          `<div class="item"><code${fragile ? ' class="warn" title="positionsbasiert"' : ''}>` +
          `${fragile ? '⚠ ' : ''}${esc(value)}</code>` +
          `<button data-kind="${kind}" data-value="${esc(value)}" title="entfernen">×</button></div>`
        );
      })
      .join('');
  }

  function render(state) {
    if (!state) return;
    stateEl.innerHTML =
      `<div class="label">Beobachtet</div>` +
      (state.selector
        ? itemsHtml([state.selector], 'selector')
        : '<div class="empty">ganze Seite</div>') +
      `<div class="label">Ignoriert (${state.ignore_selectors.length})</div>` +
      itemsHtml(state.ignore_selectors, 'ignore') +
      (state.ignore_patterns.length
        ? `<div class="label">Muster (${state.ignore_patterns.length})</div>` +
          itemsHtml(state.ignore_patterns, 'pattern')
        : '');

    for (const btn of stateEl.querySelectorAll('button[data-kind]')) {
      btn.onclick = () => window.__wwRemove(btn.dataset.kind, btn.dataset.value).then(render);
    }
  }

  async function doPreview() {
    const btn = shadow.getElementById('preview');
    btn.disabled = true;
    outEl.innerHTML = '<div class="label">Vorschau</div><pre>Rendere zweimal headless …</pre>';

    const result = await window.__wwPreview();
    btn.disabled = false;

    if (!result.ok) {
      outEl.innerHTML =
        '<div class="label">Vorschau</div>' +
        `<pre class="err">Der Server könnte das so nicht prüfen:\n${esc(result.error)}</pre>`;
      return;
    }

    const stable = result.unstable.length === 0;
    const scope = result.matches > 1 ? `${result.matches} Elemente, ` : '';
    let html =
      '<div class="label">Vorschau (headless, wie auf dem Server)</div>' +
      `<pre>${scope}${result.count} Zeilen, ${result.chars} Zeichen\n\n${esc(result.sample.join('\n'))}` +
      (result.count > result.sample.length ? '\n…' : '') +
      '</pre>';

    html += stable
      ? '<pre class="ok">Stabil: zwei Renders ergaben identischen Inhalt.</pre>'
      : `<div class="label">Instabil – das würde Fehlalarme geben</div>` +
        `<pre class="warn">${esc(result.unstable.join('\n'))}</pre>` +
        '<div class="row"><button id="accept" style="flex:1">Als ignore_patterns übernehmen</button></div>';

    outEl.innerHTML = html;
    const accept = shadow.getElementById('accept');
    if (accept) {
      accept.onclick = async () => {
        render(await window.__wwAcceptPatterns());
        doPreview();
      };
    }
  }

  // ----------------------------------------------------------------- start

  function start() {
    build();
    document.addEventListener('mouseover', onOver, true);
    document.addEventListener('click', onClick, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('scroll', hideHighlight, true);
    updateBroadHint();
    window.__wwState().then(render);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
