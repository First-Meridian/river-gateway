// Console shell: login with the admin token, sidebar navigation, hash routing.

import { admin, ApiError, session } from './api.js';
import * as accounts from './accounts.js';
import { clear, errorText, fill, h, s } from './dom.js';
import * as keys from './keys.js';
import * as logs from './logs.js';
import * as overview from './overview.js';
import * as playground from './playground.js';
import * as status from './status.js';
import { warningText } from './status.js';

const PAGES = [
  { id: 'overview', title: '总览', module: overview },
  { id: 'keys', title: '密钥管理', module: keys },
  { id: 'accounts', title: 'River 账号', module: accounts },
  { id: 'logs', title: '请求日志', module: logs },
  { id: 'playground', title: 'Playground', module: playground },
  { id: 'status', title: '状态', module: status },
];

const ICONS = {
  overview: ['M2.5 13.5h11', 'M4.5 11V8', 'M8 11V4', 'M11.5 11V6.5'],
  keys: ['M9.6 6.4 13.5 2.5', 'M11.5 4.5 13 6', 'M7.8 8.2a3.2 3.2 0 1 1-4.5 4.5 3.2 3.2 0 0 1 4.5-4.5Z'],
  accounts: ['M2.5 5.5h11v7.5h-11Z', 'M2.5 5.5 4 3h8l1.5 2.5', 'M10 9.2h1.5'],
  logs: ['M5.5 4h8', 'M5.5 8h8', 'M5.5 12h8', 'M2.5 4h.5', 'M2.5 8h.5', 'M2.5 12h.5'],
  playground: ['M2.5 3.5h11v7.5h-6l-3 2.5V11h-2Z', 'M5.5 7.2h5'],
  status: ['M1.5 8.5h3l1.8-4 3.2 8 1.8-4h3.2'],
};

function icon(name) {
  return s('svg', { viewBox: '0 0 16 16', width: 16, height: 16, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.5, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' },
    ICONS[name].map((d) => s('path', { d })));
}

const THEMES = [
  { id: 'auto', label: '跟随系统' },
  { id: 'light', label: '浅色' },
  { id: 'dark', label: '深色' },
];

function applyTheme(id) {
  if (id === 'auto') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = id;
}

const root = document.getElementById('app');
const state = { status: null, mounted: false, cleanup: null, nav: null, pageRoot: null, banner: null, chip: null };

function stopPage() {
  try {
    state.cleanup?.();
  } catch (error) {
    console.error(error);
  }
  state.cleanup = null;
}

function loginMessage(error) {
  if (error instanceof ApiError) {
    if (error.code === 'admin_disabled') return '网关没有启用管理 API：请在配置里设置 admin_token（或 .env 里的 GATEWAY_ADMIN_TOKEN）后重启网关。';
    if (error.status === 401) return '管理令牌不正确。';
    return error.message;
  }
  return errorText(error);
}

function showLogin(message = '') {
  stopPage();
  state.mounted = false;
  const token = h('input', { class: 'input', type: 'password', autocomplete: 'current-password', placeholder: 'admin_token', required: true });
  const remember = h('input', { type: 'checkbox' });
  const error = h('div', { class: 'error-text', role: 'alert' }, message);
  const button = h('button', { class: 'btn btn-primary', type: 'submit' }, '登录');
  const form = h('form', {
    onSubmit: async (event) => {
      event.preventDefault();
      if (!token.value.trim()) return;
      button.disabled = true;
      error.textContent = '';
      session.save(token.value.trim(), remember.checked);
      try {
        state.status = await admin('/status');
        if (!location.hash) location.hash = '#/overview';
        showShell();
      } catch (e) {
        session.clear();
        error.textContent = loginMessage(e);
      } finally {
        button.disabled = false;
      }
    },
  },
  h('div', { class: 'field' }, h('label', {}, '管理令牌'), token,
    h('span', { class: 'hint' }, '即网关配置中的 admin_token（默认读取 .env 里的 GATEWAY_ADMIN_TOKEN）。')),
  h('label', { class: 'check' }, remember, '在这个浏览器上记住'),
  error,
  button);

  clear(root).append(h('div', { class: 'login' }, h('div', { class: 'card login-card' },
    h('div', { class: 'brand-mark', 'aria-hidden': 'true' }, 'R'),
    h('h1', {}, 'River Gateway 控制台'),
    h('div', { class: 'muted' }, '管理网关密钥、查看用量和请求、调试模型。'),
    form)));
  token.focus();
}

function drawBanner() {
  const dismissed = sessionStorage.getItem('rgw.bannerDismissed') === '1';
  const warnings = state.status?.warnings || [];
  clear(state.banner);
  if (dismissed || !warnings.length) return;
  state.banner.append(h('div', { class: 'banner', role: 'status' },
    h('span', { class: 'banner-icon', 'aria-hidden': 'true' }, '!'),
    h('div', { style: { flex: '1' } }, warnings.map((w) => h('div', {}, warningText(w)))),
    h('button', { class: 'btn btn-sm btn-ghost', type: 'button', onClick: () => { sessionStorage.setItem('rgw.bannerDismissed', '1'); drawBanner(); } }, '知道了')));
}

function drawAccountChip() {
  const account = state.status?.account;
  if (!state.chip) return;
  clear(state.chip);
  state.chip.classList.toggle('hidden', !account);
  if (!account) return;
  fill(state.chip,
    h('span', { class: 'muted' }, 'River 账号'),
    h('strong', { title: account.name }, account.name),
    account.low_balance ? h('span', { style: { color: 'var(--critical)', fontWeight: '700' }, title: 'River 报告余额不足' }, '!') : null,
  );
}

async function refreshStatus() {
  state.status = await admin('/status');
  drawBanner();
  drawAccountChip();
}

function showShell() {
  state.mounted = true;
  let theme = localStorage.getItem('rgw.theme') || 'auto';
  const themeButton = h('button', { class: 'btn btn-ghost btn-sm', type: 'button' });
  const drawTheme = () => {
    themeButton.textContent = `主题：${THEMES.find((t) => t.id === theme).label}`;
  };
  themeButton.addEventListener('click', () => {
    theme = THEMES[(THEMES.findIndex((t) => t.id === theme) + 1) % THEMES.length].id;
    localStorage.setItem('rgw.theme', theme);
    applyTheme(theme);
    drawTheme();
  });
  drawTheme();

  state.nav = h('nav', { class: 'nav', 'aria-label': '主导航' },
    PAGES.map((page) => h('a', { href: `#/${page.id}`, dataset: { page: page.id } }, h('span', { class: 'nav-icon' }, icon(page.id)), page.title)));
  state.pageRoot = h('div');
  state.banner = h('div');
  state.chip = h('a', { class: 'account-chip', href: '#/accounts', title: '切换 River 账号' });
  clear(root).append(h('div', { class: 'shell' },
    h('aside', { class: 'sidebar' },
      h('div', { class: 'brand' }, h('div', { class: 'brand-mark', 'aria-hidden': 'true' }, 'R'),
        h('div', {}, h('div', { class: 'brand-name' }, 'River Gateway'), h('div', { class: 'brand-sub' }, `v${state.status.version}`))),
      state.chip,
      state.nav,
      h('div', { class: 'sidebar-foot' },
        themeButton,
        h('button', { class: 'btn btn-ghost btn-sm', type: 'button', onClick: () => { session.clear(); showLogin(); } }, '退出登录'))),
    h('main', { class: 'main' }, state.banner, state.pageRoot)));
  drawBanner();
  drawAccountChip();
  route();
}

function route() {
  if (!state.mounted) return;
  const id = location.hash.replace(/^#\/?/, '').split('?')[0] || 'overview';
  const page = PAGES.find((p) => p.id === id) || PAGES[0];
  stopPage();
  for (const link of state.nav.querySelectorAll('a')) {
    if (link.dataset.page === page.id) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  document.title = `${page.title} · River Gateway`;
  clear(state.pageRoot);
  window.scrollTo(0, 0);
  const ctx = {
    get status() {
      return state.status;
    },
    set status(value) {
      state.status = value;
      drawBanner();
      drawAccountChip();
    },
    refreshStatus,
  };
  try {
    state.cleanup = page.module.render(state.pageRoot, ctx) || null;
  } catch (error) {
    console.error(error);
    state.pageRoot.append(h('div', { class: 'card error-text' }, `页面出错：${errorText(error)}`));
  }
}

async function boot() {
  applyTheme(localStorage.getItem('rgw.theme') || 'auto');
  window.addEventListener('hashchange', route);
  window.addEventListener('rgw:unauthorized', () => {
    if (!state.mounted) return;
    session.clear();
    showLogin('登录已失效（管理令牌可能已更改），请重新输入。');
  });
  if (!session.token) {
    showLogin();
    return;
  }
  try {
    state.status = await admin('/status');
    showShell();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) session.clear();
    showLogin(loginMessage(error));
  }
}

boot();
