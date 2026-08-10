/* webwatcher selector builder.
 *
 * Turns a DOM element into the most stable CSS selector it can find. Kept apart
 * from picker.js because two very different callers need exactly this much and
 * nothing else:
 *   - picker.js, the overlay UI of `webwatcher pick` (headed browser)
 *   - web/picker.py, the server side picker, which injects only this file and
 *     calls __wwBuildSelector(document.elementFromPoint(x, y))
 * Everything here is pure: no bindings, no UI, safe to inject anywhere. */
(() => {
  if (window.__wwSelectorLoaded) return;
  window.__wwSelectorLoaded = true;

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

  window.__wwBuildSelector = buildSelector;
  window.__wwBroadSelector = broadSelector;
  window.__wwCountFor = countFor;
})();
