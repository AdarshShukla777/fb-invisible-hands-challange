"""In-page JavaScript that describes elements for page history and healing.

Privacy: it never reads form values (input.value). The only attribute-derived labels are
aria/label/placeholder/title texts and the value attribute of *button-type* inputs, which
is their visible caption. Link hrefs are reduced to their path (no query/fragment)."""
from __future__ import annotations

_LIB = r"""
const TESTID_ATTRS = ['data-testid', 'data-test', 'data-test-id', 'data-qa', 'data-cy'];
const INPUT_ROLES = {button: 'button', submit: 'button', reset: 'button', image: 'button', checkbox: 'checkbox',
  radio: 'radio', range: 'slider', number: 'spinbutton', search: 'searchbox'};
const NON_TEXT_INPUTS = ['button', 'submit', 'reset', 'image', 'checkbox', 'radio', 'range', 'color', 'file', 'hidden'];
const clean = (s, n) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n || 120);
function roleOf(el) {
  const r = el.getAttribute('role');
  if (r) return r.trim().split(/\s+/)[0];
  const t = el.tagName.toLowerCase();
  if (t === 'a') return el.hasAttribute('href') ? 'link' : '';
  if (t === 'button' || t === 'summary') return 'button';
  if (t === 'select') return (el.multiple || el.size > 1) ? 'listbox' : 'combobox';
  if (t === 'textarea') return 'textbox';
  if (t === 'input') {
    const ty = (el.getAttribute('type') || 'text').toLowerCase();
    if (ty === 'hidden') return '';
    if (ty in INPUT_ROLES) return INPUT_ROLES[ty];
    return el.hasAttribute('list') ? 'combobox' : 'textbox';
  }
  if (/^h[1-6]$/.test(t)) return 'heading';
  if (t === 'img') return 'img';
  if (el.isContentEditable) return 'textbox';
  return '';
}
function labelOf(el) {
  if (!el.labels) return '';
  return clean(Array.from(el.labels).map(l => l.innerText).join(' '));
}
function nameOf(el) {
  const lb = el.getAttribute('aria-labelledby');
  if (lb) {
    const t = clean(lb.split(/\s+/).map(id => { const n = document.getElementById(id); return n ? n.innerText : ''; }).join(' '));
    if (t) return t;
  }
  const al = clean(el.getAttribute('aria-label'));
  if (al) return al;
  const t = el.tagName.toLowerCase();
  if (t === 'input' || t === 'textarea' || t === 'select') {
    const ty = (el.getAttribute('type') || '').toLowerCase();
    if (['button', 'submit', 'reset'].includes(ty)) return clean(el.getAttribute('value') || (ty === 'submit' ? 'Submit' : ''));
    if (ty === 'image') return clean(el.getAttribute('alt'));
    return labelOf(el) || clean(el.getAttribute('placeholder') || el.getAttribute('title'));
  }
  if (t === 'img') return clean(el.getAttribute('alt'));
  const text = clean(el.innerText || el.textContent);
  if (text) return text;
  const img = el.querySelector('img[alt]');
  return img ? clean(img.getAttribute('alt')) : clean(el.getAttribute('title'));
}
function isVisible(el) {
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return false;
  const s = getComputedStyle(el);
  return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0;
}
function contextOf(el) {
  const form = el.closest('form');
  const land = el.closest('nav,main,header,footer,aside,dialog,[role=dialog],[role=navigation],[role=main],[role=banner],[role=contentinfo],[role=region],section[aria-label]');
  let heading = '';
  let node = el.parentElement;
  for (let depth = 0; node && depth < 6 && !heading; depth++, node = node.parentElement) {
    const h = node.querySelector('h1,h2,h3,h4,h5,h6,legend');
    if (h && h !== el) heading = clean(h.innerText, 80);
  }
  return {
    form: form ? clean(form.getAttribute('id') || form.getAttribute('name') || form.getAttribute('aria-label'), 60) : '',
    landmark: land ? clean(land.getAttribute('aria-label') || land.getAttribute('role') || land.tagName.toLowerCase(), 60) : '',
    heading,
  };
}
function testidOf(el) {
  for (const a of TESTID_ATTRS) { const v = el.getAttribute(a); if (v) return [a, v]; }
  return ['', ''];
}
function cssPath(el) {
  const parts = [];
  let n = el;
  while (n && n.nodeType === 1 && parts.length < 6) {
    if (n.id && document.querySelectorAll('#' + CSS.escape(n.id)).length === 1) { parts.unshift('#' + CSS.escape(n.id)); break; }
    let p = n.tagName.toLowerCase();
    const par = n.parentElement;
    if (par) {
      const same = Array.from(par.children).filter(c => c.tagName === n.tagName);
      if (same.length > 1) p += ':nth-of-type(' + (same.indexOf(n) + 1) + ')';
    }
    parts.unshift(p);
    n = par;
  }
  return parts.join(' > ');
}
const ICON_CLASS = /^(bi|fa|fas|far|mdi|icon|glyphicon|ti|ri|la|feather|material-icons?)-[\w-]+$/;
function iconOf(el) {
  const nodes = [el, ...el.querySelectorAll('i, svg, span, use')].slice(0, 6);
  for (const n of nodes) {
    const cls = (n.getAttribute('class') || '').split(/\s+/).filter(c => ICON_CLASS.test(c));
    if (cls.length) return cls[cls.length - 1];
    if (n.classList && (n.classList.contains('material-icons') || n.classList.contains('material-symbols-outlined'))) return clean(n.textContent, 40);
  }
  return '';
}
const ROW_SEL = '[role=row], tr';
function rowOf(el) {
  const row = el.closest(ROW_SEL);
  if (!row || row === el) return ['', 0];
  const table = row.closest('[role=table], [role=grid], table') || document;
  const dataRows = Array.from(table.querySelectorAll(ROW_SEL)).filter(r => r.querySelector('[role=cell], [role=gridcell], td'));
  return [clean(row.innerText.replace(/\s*\n\s*/g, ' | '), 100), dataRows.indexOf(row) + 1];
}
function describe(el) {
  const t = el.tagName.toLowerCase();
  const ty = t === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : '';
  const [tidAttr, tid] = testidOf(el);
  const disabled = el.disabled === true || el.getAttribute('aria-disabled') === 'true';
  const formField = t === 'input' || t === 'textarea' || t === 'select';
  const editable = ((t === 'input' && !NON_TEXT_INPUTS.includes(ty)) || t === 'textarea' || el.isContentEditable) && !el.readOnly;
  const href = t === 'a' ? (el.getAttribute('href') || '') : '';
  const [row, rowIndex] = rowOf(el);
  return {
    icon: iconOf(el), row, rowIndex,
    tag: t, type: ty, role: roleOf(el), name: nameOf(el),
    label: formField ? labelOf(el) : '',
    placeholder: clean(el.getAttribute('placeholder')),
    testid: tid, testidAttr: tidAttr,
    id: clean(el.id, 80), nameAttr: clean(el.getAttribute('name'), 80),
    text: formField ? '' : clean(el.innerText, 80),
    href: href ? clean(href.split(/[?#]/)[0], 120) : '',
    visible: isVisible(el), enabled: !disabled, editable: editable && !disabled,
    clickable: el.hasAttribute('onclick') || typeof el.onclick === 'function' || (el.hasAttribute('tabindex') && el.getAttribute('tabindex') !== '-1'),
    context: contextOf(el), css: cssPath(el),
  };
}
"""

_SELECTOR = (
    "a[href],button,input:not([type=hidden]),select,textarea,summary,[role],"
    "[data-testid],[data-test],[data-test-id],[data-qa],[data-cy],"
    "[contenteditable=''],[contenteditable='true'],[onclick],[tabindex]:not([tabindex='-1']),"
    "h1,h2,h3,h4,h5,h6"
)

# -- visual overlay (visible-browser runs and recordings) -------------------------------------
# Drawn in a *closed* shadow root with pointer-events: none, so it never intercepts clicks and is
# invisible to Playwright text/role locators, document.body.innerText and the element inventory.
_OVERLAY = r"""
if (!window.__uitestOverlay) {
  const host = document.createElement('uitest-overlay');
  host.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;display:block;';
  const root = host.attachShadow({mode: 'closed'});
  root.innerHTML = `<style>
    .box{position:fixed;border:2px solid;border-radius:3px;box-sizing:border-box}
    .tag{position:absolute;top:-15px;left:-2px;font:bold 11px/13px monospace;color:#fff;padding:0 3px;border-radius:3px}
    .target{position:fixed;border:3px solid #ff2d55;border-radius:4px;box-sizing:border-box;
            box-shadow:0 0 0 5px rgba(255,45,85,.25);transition:all .2s}
    .label{position:absolute;top:100%;left:-3px;margin-top:4px;background:#ff2d55;color:#fff;
           font:bold 12px/16px sans-serif;padding:1px 6px;border-radius:3px;white-space:nowrap}
    .cursor{position:fixed;left:0;top:0;width:26px;height:26px;transition:transform .35s ease-out;
            transform:translate(-60px,-60px);filter:drop-shadow(0 1px 2px rgba(0,0,0,.5))}
    .banner{position:fixed;top:10px;right:10px;max-width:45%;padding:7px 12px;border-radius:6px;
            font:13px/18px sans-serif;color:#fff;background:rgba(20,24,40,.9)}
  </style><div id="boxes"></div><div id="target"></div><div id="banner"></div>
  <svg class="cursor" id="cursor" viewBox="0 0 24 24"><path d="M4 2l6.5 19 2.6-7.6L21 11z"
       fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
  document.documentElement.appendChild(host);
  const $ = (id) => root.getElementById(id);
  const COLORS = ['#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4', '#0099aa', '#d6338f', '#6b8e23'];
  const place = (node, r) => Object.assign(node.style, {left: r.left + 'px', top: r.top + 'px',
                                                         width: r.width + 'px', height: r.height + 'px'});
  window.__uitestOverlay = {
    boxes(ids) {
      const layer = $('boxes'); layer.innerHTML = '';
      for (const i of ids) {
        const el = (window.__uitestCands || [])[i]; if (!el) continue;
        const r = el.getBoundingClientRect(); if (r.width <= 0 || r.height <= 0) continue;
        const c = COLORS[i % COLORS.length], box = document.createElement('div');
        box.className = 'box'; box.style.borderColor = c; place(box, r);
        box.innerHTML = `<span class="tag" style="background:${c}">${i}</span>`;
        layer.appendChild(box);
      }
    },
    clearBoxes() { $('boxes').innerHTML = ''; },
    point(el, text) {
      el.scrollIntoView({block: 'center', inline: 'center'});
      const r = el.getBoundingClientRect(), t = document.createElement('div');
      t.className = 'target'; place(t, r);
      const l = document.createElement('span'); l.className = 'label'; l.textContent = text; t.appendChild(l);
      $('target').replaceChildren(t);
      $('cursor').style.transform = `translate(${r.left + r.width / 2}px, ${r.top + r.height / 2}px)`;
    },
    clearTarget() { $('target').innerHTML = ''; },
    banner(text, color) {
      const b = $('banner'); b.innerHTML = '';
      const d = document.createElement('div'); d.className = 'banner'; d.textContent = text;
      if (color) d.style.background = color;
      b.appendChild(d); clearTimeout(this._t); this._t = setTimeout(() => { b.innerHTML = ''; }, 3000);
    },
  };
}
"""
OVERLAY_BOXES_JS = "(ids) => {" + _OVERLAY + " window.__uitestOverlay.boxes(ids); }"
OVERLAY_CLEAR_JS = "() => { if (window.__uitestOverlay) { window.__uitestOverlay.clearBoxes(); window.__uitestOverlay.clearTarget(); } }"
OVERLAY_POINT_JS = "(el, text) => {" + _OVERLAY + " window.__uitestOverlay.point(el, text); }"
OVERLAY_BANNER_JS = "([text, color]) => {" + _OVERLAY + " window.__uitestOverlay.banner(text, color); }"

# Whether text is visible on the page, also when it spans several elements ("73 | warning | Overall
# health"): compares against the rendered text with whitespace collapsed, case-insensitively.
PAGE_HAS_TEXT_JS = (
    "(v) => { if (!document.body) return false;"
    " const n = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();"
    " const t = n(document.body.innerText), w = n(v);"
    # inline labels can render glued together ("73warning"), so also compare without spaces
    " return t.includes(w) || t.replace(/ /g, '').includes(w.replace(/ /g, '')); }"
)

# Visible page text for the agent (whitespace collapsed, truncated by the caller).
PAGE_TEXT_JS = "() => (document.body ? document.body.innerText : '').replace(/[ \\t]+/g, ' ').replace(/\\s*\\n\\s*/g, ' | ')"

# Returns {title, elements, truncated}. Keeps the element references in window.__uitestCands so a
# healing candidate can later be matched back to the exact DOM node (see SAME_ELEMENT_JS).
INVENTORY_JS = (
    "(max) => {" + _LIB + f"""
  const all = Array.from(document.querySelectorAll("{_SELECTOR}"));
  const els = all.slice(0, max);
  window.__uitestCands = els;
  return {{ title: document.title, truncated: all.length > max, elements: els.map((el, i) => Object.assign(describe(el), {{ idx: i }})) }};
}}"""
)

DESCRIBE_JS = "(el) => {" + _LIB + " return describe(el); }"

SAME_ELEMENT_JS = "(el, idx) => Array.isArray(window.__uitestCands) && window.__uitestCands[idx] === el"

# Lists (li, tr, ARIA rows/items/options, articles) grouped by their container, so the agent runner
# can tell which list an action changed and check every item of it (check_items). A container key is
# a short tag#id.class path; header rows (only th / columnheader cells) are not items.
_LISTS = r"""
const ITEM_SEL = 'li, tr, [role=row], [role=listitem], [role=option], [role=article], article';
const BOX_SEL = 'ol, ul, tbody, table, [role=list], [role=listbox], [role=grid], [role=table], [role=rowgroup], [role=feed]';
const shown = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
  return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
const header = (e) => !!e.querySelector('th, [role=columnheader]') && !e.querySelector('td, [role=cell], [role=gridcell]');
const first = (e) => (typeof e.className === 'string' ? e.className.trim().split(/\s+/)[0] : '');
const keyOf = (box) => { const p = [];
  for (let e = box; e && e !== document.body && p.length < 6; e = e.parentElement)
    p.unshift(e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + (first(e) ? '.' + first(e) : ''));
  return p.join('>'); };
const txt = (e) => [e.innerText || '', ...Array.from(e.querySelectorAll('[aria-label], [title], img[alt]'))
  .map((d) => d.getAttribute('aria-label') || d.getAttribute('title') || d.getAttribute('alt') || '')]
  .join(' ').replace(/\s+/g, ' ').trim();
// Lists built from plain elements (e.g. <div class="card"> x N): at least 3 visible look-alike siblings
// (same tag and class) with text, that are not controls or form rows.
const LOOKALIKE_MIN = 3;
const sig = (c) => c.tagName + '.' + (typeof c.className === 'string' ? c.className.trim() : '');
const plainItem = (c) => typeof c.className === 'string' && c.className.trim() && shown(c)
  && !/^(A|BUTTON|INPUT|LABEL|OPTION|SELECT|TEXTAREA|SCRIPT|STYLE)$/.test(c.tagName)
  && !c.querySelector('input:not([type=checkbox]):not([type=radio]), select, textarea') && !!(c.textContent || '').trim();
function plainLists() {
  const out = [];
  for (const box of document.querySelectorAll('body *')) {
    if (box.childElementCount < LOOKALIKE_MIN || box.matches(BOX_SEL)) continue;
    const groups = {};
    for (const c of box.children) if (!c.matches(ITEM_SEL) && plainItem(c)) (groups[sig(c)] = groups[sig(c)] || []).push(c);
    const best = Object.values(groups).sort((a, b) => b.length - a.length)[0];
    if (best && best.length >= LOOKALIKE_MIN) out.push([box, best]);
  }
  return out;
}
const itemsOf = (box) => { const kids = Array.from(box.children);
  const marked = kids.filter((c) => c.matches(ITEM_SEL));
  if (marked.length) return marked.filter((c) => shown(c) && !header(c)).map(txt).filter(Boolean);
  const groups = {};
  for (const c of kids) if (plainItem(c)) (groups[sig(c)] = groups[sig(c)] || []).push(c);
  const best = Object.values(groups).sort((a, b) => b.length - a.length)[0];
  return (best || kids.filter(shown)).map(txt).filter(Boolean); };
"""
# {key: [item count, signature of the item texts]}: a filter can swap the items and keep the count.
LISTS_JS = "() => {" + _LISTS + """
  const out = {};
  for (const it of document.querySelectorAll(ITEM_SEL)) {
    const box = it.parentElement;
    if (!box || !shown(it) || header(it)) continue;
    add(keyOf(box), it);
  }
  for (const [box, items] of plainLists()) { const k = keyOf(box); if (!(k in out)) items.forEach((it) => add(k, it)); }
  return out;
  function add(k, it) {
    const t = txt(it), o = out[k] || (out[k] = [0, 5381]);
    o[0] += 1;
    for (let i = 0; i < t.length; i++) o[1] = ((o[1] * 33) ^ t.charCodeAt(i)) >>> 0;
    o[1] = ((o[1] * 33) ^ 124) >>> 0;
  } }"""
# The items of the list that contains el (el may be an item, something inside one, or the list itself).
LIST_ITEMS_OF_JS = "(el) => {" + _LISTS + """
  if (el.matches(BOX_SEL)) {
    const box = el.tagName === 'TABLE' && el.tBodies.length ? el.tBodies[0] : el;
    return {key: keyOf(box), found: true, items: itemsOf(box)};
  }
  const item = el.closest(ITEM_SEL);
  if (item && item.parentElement) return {key: keyOf(item.parentElement), found: true, items: itemsOf(item.parentElement)};
  for (let e = el; e && e.parentElement && e !== document.body; e = e.parentElement) {  // a plain card or row
    const box = e.parentElement, like = Array.from(box.children).filter((c) => sig(c) === sig(e) && shown(c));
    if (like.length >= 2) return {key: keyOf(box), found: true, items: like.map(txt).filter(Boolean)};
  }
  return {key: keyOf(el.parentElement), found: true, items: [txt(el)].filter(Boolean)}; }"""
LIST_ITEMS_JS = "(key) => {" + _LISTS + """
  const tag = key.split('>').pop().split(/[.#]/)[0];
  const box = Array.from(document.getElementsByTagName(tag)).find((e) => keyOf(e) === key);
  return box ? {key, found: true, items: itemsOf(box)} : {key, found: false, items: []}; }"""

# Where an element is, measured just before it is acted on (layout tracking). Page coordinates
# (viewport + scroll; viewport only for fixed/sticky elements), size, the viewport, the region it
# sits in (landmark and form, as in contextOf) and its offset inside that region's box, so a
# button that moved inside its form can be told apart from a form that moved as a whole.
GEOMETRY_JS = "(el) => {" + _LIB + r"""
  const r = el.getBoundingClientRect();
  let fixed = false;
  for (let e = el; e && e !== document.body; e = e.parentElement) {
    const p = getComputedStyle(e).position;
    if (p === 'fixed' || p === 'sticky') { fixed = true; break; }
  }
  const sx = fixed ? 0 : window.scrollX, sy = fixed ? 0 : window.scrollY;
  const REGION = 'form,nav,main,header,footer,aside,dialog,[role=dialog],[role=navigation],[role=main],[role=banner],'
    + '[role=contentinfo],[role=region],section[aria-label]';
  const box = (el.parentElement && el.parentElement.closest(REGION)) || document.body;
  const b = box.getBoundingClientRect(), c = contextOf(el);
  const region = [c.landmark, c.form ? 'form ' + c.form : (box.tagName === 'FORM' ? 'form' : '')].filter(Boolean).join(' / ') || 'page';
  const R = Math.round;
  return {x: R(r.left + sx), y: R(r.top + sy), w: R(r.width), h: R(r.height), vw: window.innerWidth, vh: window.innerHeight,
          fixed, region, ox: R(r.left - b.left), oy: R(r.top - b.top)};
}"""
