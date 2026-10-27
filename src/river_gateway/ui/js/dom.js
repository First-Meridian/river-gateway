// DOM helpers. Data always goes in as text nodes or attributes, never as HTML.

function appendChildren(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

/** Create an HTML element: h('button', {class: 'btn', onClick}, 'Save'). */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (typeof value !== 'string' && key in el) el[key] = value;
    else el.setAttribute(key, value === true ? '' : String(value));
  }
  appendChildren(el, children);
  return el;
}

const SVG_NS = 'http://www.w3.org/2000/svg';

/** Create an SVG element. */
export function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2).toLowerCase(), value);
    else el.setAttribute(key, String(value));
  }
  appendChildren(el, children);
  return el;
}

export function clear(el) {
  el.replaceChildren();
  return el;
}

/** Replace an element's children, skipping null/false (Element.append would print "null"). */
export function fill(el, ...children) {
  el.replaceChildren();
  appendChildren(el, children);
  return el;
}

// ---- formatting -------------------------------------------------------------

const integer = new Intl.NumberFormat('zh-CN');
const compactFormat = new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 });
const pad = (n) => String(n).padStart(2, '0');

export const fmt = {
  int: (n) => (n === null || n === undefined ? '–' : integer.format(n)),
  compact: (n) => (n === null || n === undefined ? '–' : Math.abs(n) < 10000 ? integer.format(Math.round(n)) : compactFormat.format(n)),
  usd(n) {
    if (n === null || n === undefined) return '–';
    if (n === 0) return '$0';
    const abs = Math.abs(n);
    return '$' + n.toFixed(abs >= 1 ? 2 : abs >= 0.01 ? 4 : 6);
  },
  /** Short currency for axis ticks: $0.04, $0.0003, $1.5 (no trailing zeros). */
  usdAxis(n) {
    if (!n) return '$0';
    return '$' + Number(n.toPrecision(3)).toLocaleString('en-US', { maximumFractionDigits: 8 });
  },
  pct(ratio) {
    if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) return '–';
    const value = ratio * 100;
    return (value === 100 || value === 0 ? value.toFixed(0) : value.toFixed(1)) + '%';
  },
  ms(n) {
    if (n === null || n === undefined) return '–';
    return n < 1000 ? `${Math.round(n)} ms` : `${(n / 1000).toFixed(n < 10000 ? 2 : 1)} s`;
  },
  dateTime(ts) {
    if (!ts) return '–';
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  },
  date(ts) {
    if (!ts) return '–';
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  },
  shortTime(ts) {
    const d = new Date(ts * 1000);
    const today = new Date();
    const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    return d.toDateString() === today.toDateString() ? time : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${time}`;
  },
  relative(ts) {
    if (!ts) return '从未';
    const seconds = Date.now() / 1000 - ts;
    if (seconds < 0) {
      const ahead = -seconds;
      if (ahead < 3600) return `${Math.max(1, Math.round(ahead / 60))} 分钟后`;
      if (ahead < 86400) return `${Math.round(ahead / 3600)} 小时后`;
      return `${Math.round(ahead / 86400)} 天后`;
    }
    if (seconds < 60) return '刚刚';
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
    return `${Math.floor(seconds / 86400)} 天前`;
  },
  duration(seconds) {
    const d = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (d) return `${d} 天 ${hours} 小时`;
    if (hours) return `${hours} 小时 ${minutes} 分钟`;
    return `${minutes} 分钟`;
  },
};

export const dayKey = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
export const hourKey = (d) => `${dayKey(d)} ${pad(d.getHours())}:00`;

// ---- feedback ---------------------------------------------------------------

export function toast(message, kind = 'info') {
  const icons = { info: 'ℹ', success: '✓', error: '✕' };
  const colors = { info: 'var(--accent)', success: 'var(--good)', error: 'var(--critical)' };
  const el = h('div', { class: 'toast' }, h('span', { style: { color: colors[kind], fontWeight: '700' } }, icons[kind]), message);
  document.getElementById('toasts').append(el);
  setTimeout(() => el.remove(), kind === 'error' ? 6000 : 3500);
}

/** Open a modal dialog; returns {dialog, close}. */
export function openModal({ title, body, actions = [], wide = false, onClose }) {
  const dialog = h(
    'dialog',
    { class: 'modal' + (wide ? ' wide' : '') },
    h('div', { class: 'modal-head' }, h('h2', {}, title), h('button', { class: 'icon-btn', type: 'button', 'aria-label': '关闭', onClick: () => dialog.close() }, '×')),
    h('div', { class: 'modal-body' }, body),
    actions.length ? h('div', { class: 'modal-foot' }, actions) : null,
  );
  dialog.addEventListener('close', () => {
    dialog.remove();
    onClose?.();
  });
  document.body.append(dialog);
  dialog.showModal();
  return { dialog, close: () => dialog.close() };
}

export function confirmDialog({ title, message, confirmText = '确定', danger = false }) {
  return new Promise((resolve) => {
    let answered = false;
    const finish = (value) => {
      answered = true;
      resolve(value);
      modal.close();
    };
    const modal = openModal({
      title,
      body: h('p', { style: { margin: '0' } }, message),
      actions: [
        h('button', { class: 'btn', type: 'button', onClick: () => finish(false) }, '取消'),
        h('button', { class: 'btn ' + (danger ? 'btn-danger' : 'btn-primary'), type: 'button', onClick: () => finish(true) }, confirmText),
      ],
      onClose: () => answered || resolve(false),
    });
  });
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = h('textarea', { style: { position: 'fixed', opacity: '0' } }, text);
    document.body.append(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }
  toast('已复制到剪贴板', 'success');
}

/** A status badge: icon + label, colour only on the icon. */
export function badge(kind, icon, label, title) {
  return h('span', { class: `badge badge-${kind}`, title }, h('span', { class: 'badge-icon', 'aria-hidden': 'true' }, icon), label);
}

export function emptyState(text) {
  return h('div', { class: 'empty' }, text);
}

export function errorText(error) {
  return error?.message || String(error);
}
