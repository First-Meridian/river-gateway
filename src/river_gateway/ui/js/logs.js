// 请求日志: newest first, filter by key / errors, auto refresh, paging, detail view.

import { admin } from './api.js';
import { badge, clear, copyText, emptyState, errorText, fmt, h, openModal, toast } from './dom.js';

const PAGE_SIZE = 50;

export function statusBadge(row) {
  const status = row.status;
  if (status >= 200 && status < 300) {
    return row.error ? badge('warning', '!', `${status} 未完成`, row.error) : badge('good', '✓', String(status));
  }
  if (status === 499) return badge('neutral', '⊘', '499 客户端断开', row.error || '');
  if (status === 429) return badge('serious', '!', '429 限流', row.error || '');
  if (status >= 500) return badge('critical', '✕', String(status), row.error || '');
  return badge('serious', '!', String(status), row.error || '');
}

function modelCell(row) {
  const rerouted = row.target_model && row.target_model !== row.model;
  return h('div', {},
    h('div', { class: 'cell-main mono' }, row.model || '–'),
    rerouted ? h('div', { class: 'cell-sub mono' }, `→ ${row.target_model}`) : null,
    row.upstream && row.upstream !== 'river' ? h('div', { class: 'cell-sub' }, `@ ${row.upstream}`) : null);
}

function copyable(value) {
  if (!value) return h('span', { class: 'muted' }, '–');
  return h('span', {}, h('span', { class: 'mono' }, value), ' ',
    h('button', { class: 'btn btn-sm btn-ghost', type: 'button', onClick: () => copyText(value) }, '复制'));
}

function showDetail(row) {
  const items = [
    ['时间', fmt.dateTime(row.ts)],
    ['状态', statusBadge(row)],
    ['密钥', row.key_name ? `${row.key_name}（${row.key_id}）` : row.key_id || '–'],
    ['接口', `${row.endpoint === 'chat' ? 'POST /v1/chat/completions' : 'POST /v1/responses'}${row.stream ? '（流式）' : ''}`],
    ['请求的模型', h('span', { class: 'mono' }, row.model || '–')],
    ['实际调用', h('span', { class: 'mono' }, `${row.target_model || '–'} @ ${row.upstream || '–'}`)],
    ['River 账号', row.account_id ? `${row.account_name || '已删除'}（${row.account_id}）` : '–'],
    ['尝试次数', String(row.attempts)],
    ['输入 Token', row.prompt_tokens == null ? '–' : `${fmt.int(row.prompt_tokens)}（缓存 ${fmt.int(row.cached_tokens ?? 0)}）`],
    ['输出 Token', row.completion_tokens == null ? '–' : `${fmt.int(row.completion_tokens)}（推理 ${fmt.int(row.reasoning_tokens ?? 0)}）`],
    ['估算费用', fmt.usd(row.cost_usd)],
    ['总耗时', fmt.ms(row.latency_ms)],
    ['首包时间（第一个流式事件）', fmt.ms(row.ttft_ms)],
    ['网关请求 ID', copyable(row.request_id)],
    ['上游请求 ID', copyable(row.upstream_request_id)],
    ['错误', row.error ? h('span', { class: 'error-text' }, row.error) : h('span', { class: 'muted' }, '无')],
  ];
  openModal({
    title: '请求详情',
    wide: true,
    body: h('dl', { class: 'kv' }, items.flatMap(([label, value]) => [h('dt', {}, label), h('dd', {}, value)])),
  });
}

export function render(container) {
  const query = new URLSearchParams(location.hash.split('?')[1] || '');
  let keyId = query.get('key') || '';
  let errorsOnly = query.get('errors') === '1';
  let rows = [];
  let exhausted = false;
  let timer = null;
  const abort = new AbortController();

  const keySelect = h('select', { class: 'select', style: { maxWidth: '240px' }, onChange: (e) => { keyId = e.target.value; reload(); } },
    h('option', { value: '' }, '全部密钥'));
  const errorsBox = h('input', { type: 'checkbox', checked: errorsOnly, onChange: (e) => { errorsOnly = e.target.checked; reload(); } });
  const autoBox = h('input', { type: 'checkbox', onChange: (e) => setAuto(e.target.checked) });
  const tableBox = h('div', { class: 'card' });
  const more = h('button', { class: 'btn hidden', type: 'button', onClick: () => loadMore() }, '加载更多');

  container.append(
    h('div', { class: 'page-header' },
      h('div', {}, h('h1', {}, '请求日志'), h('div', { class: 'sub' }, '每个经过网关并发往上游的请求；点击一行查看详情。'))),
    h('div', { class: 'toolbar' },
      keySelect,
      h('label', { class: 'check' }, errorsBox, '只看异常'),
      h('label', { class: 'check' }, autoBox, '每 5 秒自动刷新'),
      h('span', { class: 'spacer' }),
      h('button', { class: 'btn btn-sm', type: 'button', onClick: () => reload() }, '刷新')),
    tableBox,
    h('div', { style: { textAlign: 'center', marginTop: '12px' } }, more),
  );

  const fetchPage = (beforeId) =>
    admin('/usage/recent', { params: { limit: PAGE_SIZE, key_id: keyId, errors_only: errorsOnly, before_id: beforeId }, signal: abort.signal })
      .then((data) => data.data);

  function draw() {
    clear(tableBox);
    more.classList.toggle('hidden', exhausted || !rows.length);
    if (!rows.length) {
      tableBox.append(emptyState(errorsOnly ? '没有异常请求。' : '还没有请求记录。'));
      return;
    }
    tableBox.append(h('div', { class: 'table-wrap' }, h('table', { class: 'table' },
      h('thead', {}, h('tr', {},
        h('th', {}, '时间'), h('th', {}, '密钥'), h('th', {}, '模型'), h('th', {}, '状态'), h('th', {}, '类型'),
        h('th', { class: 'num' }, 'Token 入/出'), h('th', { class: 'num' }, '费用'), h('th', { class: 'num' }, '耗时'))),
      h('tbody', {}, rows.map((row) => h('tr', {
        class: 'clickable',
        tabindex: 0,
        onClick: () => showDetail(row),
        onKeydown: (e) => e.key === 'Enter' && showDetail(row),
      },
      h('td', { class: 'small nowrap' }, h('span', { title: fmt.dateTime(row.ts) }, fmt.shortTime(row.ts))),
      h('td', { class: 'small' }, row.key_name || h('span', { class: 'muted', title: row.key_id || '' }, row.key_id ? '已删除' : '–')),
      h('td', {}, modelCell(row)),
      h('td', {}, statusBadge(row)),
      h('td', { class: 'small nowrap' }, row.endpoint === 'chat' ? 'Chat' : 'Responses', row.stream ? ' · 流式' : '',
        row.attempts > 1 ? h('div', { class: 'cell-sub' }, `尝试 ${row.attempts} 次`) : null),
      h('td', { class: 'num small' }, row.prompt_tokens == null ? '–' : `${fmt.int(row.prompt_tokens)} / ${fmt.int(row.completion_tokens)}`),
      h('td', { class: 'num small' }, fmt.usd(row.cost_usd)),
      h('td', { class: 'num small' }, fmt.ms(row.latency_ms), row.ttft_ms != null && row.stream ? h('div', { class: 'cell-sub' }, `首包 ${fmt.ms(row.ttft_ms)}`) : null),
      ))))));
  }

  async function reload() {
    tableBox.classList.add('is-loading');
    try {
      rows = await fetchPage();
      exhausted = rows.length < PAGE_SIZE;
      draw();
    } catch (e) {
      if (e.name !== 'AbortError') toast(`加载日志失败：${errorText(e)}`, 'error');
    } finally {
      tableBox.classList.remove('is-loading');
    }
  }

  async function loadMore() {
    if (!rows.length) return;
    more.disabled = true;
    try {
      const page = await fetchPage(rows[rows.length - 1].id);
      rows.push(...page);
      exhausted = page.length < PAGE_SIZE;
      draw();
    } catch (e) {
      if (e.name !== 'AbortError') toast(errorText(e), 'error');
    } finally {
      more.disabled = false;
    }
  }

  async function refreshNewest() {
    try {
      const page = await fetchPage();
      const newestId = rows.length ? rows[0].id : 0;
      const newer = page.filter((row) => row.id > newestId);
      if (newer.length === PAGE_SIZE && rows.length) {
        // A full page of new rows may hide a gap; start over.
        rows = page;
        exhausted = page.length < PAGE_SIZE;
      } else {
        rows = [...newer, ...rows];
      }
      if (newer.length) draw();
    } catch (e) {
      if (e.name !== 'AbortError') setAuto(false);
    }
  }

  function setAuto(on) {
    clearInterval(timer);
    timer = on ? setInterval(refreshNewest, 5000) : null;
    autoBox.checked = on;
  }

  admin('/keys', { signal: abort.signal })
    .then(({ data }) => {
      for (const key of data.sort((a, b) => a.name.localeCompare(b.name))) {
        keySelect.append(h('option', { value: key.id, selected: key.id === keyId }, `${key.name}（${key.id}）`));
      }
    })
    .catch(() => {});
  reload();
  return () => {
    abort.abort();
    clearInterval(timer);
  };
}
