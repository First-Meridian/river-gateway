// 密钥管理: list, create (secret shown once), edit limits, enable/disable, delete.

import { admin, session } from './api.js';
import { meter } from './charts.js';
import { badge, clear, confirmDialog, copyText, emptyState, errorText, fmt, h, openModal, toast } from './dom.js';

export function keyState(key) {
  if (!key.enabled) return { kind: 'neutral', icon: '⏸', label: '已停用' };
  if (key.expires_at && key.expires_at * 1000 <= Date.now()) return { kind: 'warning', icon: '⌛', label: '已过期' };
  if (key.budget_usd !== null && key.spent_usd >= key.budget_usd) return { kind: 'critical', icon: '!', label: '预算用尽' };
  return { kind: 'good', icon: '✓', label: '正常' };
}

function field(label, control, hint, full = false) {
  return h('div', { class: 'field' + (full ? ' full' : '') }, h('span', { class: 'label' }, label), control, hint ? h('span', { class: 'hint' }, hint) : null);
}

/** Chip input for model patterns; an empty list means every model (*). */
function modelPicker(initial, suggestions) {
  const values = [...initial];
  const chips = h('div', { class: 'chips' });
  const listId = 'model-list-' + Math.random().toString(36).slice(2);
  const input = h('input', { class: 'input', list: listId, placeholder: '模型名或通配符（如 deepseek-ai/*），回车添加' });
  const add = () => {
    for (const part of input.value.split(',')) {
      const value = part.trim();
      if (value && !values.includes(value)) values.push(value);
    }
    input.value = '';
    draw();
  };
  const draw = () => {
    clear(chips);
    if (!values.length) chips.append(h('span', { class: 'muted small' }, '全部模型（*）'));
    for (const value of values) {
      chips.append(h('span', { class: 'chip' }, h('span', { title: value }, value),
        h('button', { type: 'button', 'aria-label': `移除 ${value}`, onClick: () => { values.splice(values.indexOf(value), 1); draw(); } }, '×')));
    }
  };
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ',') {
      event.preventDefault();
      add();
    }
  });
  input.addEventListener('change', () => suggestions.includes(input.value) && add());
  draw();
  return {
    el: h('div', { class: 'field' }, chips, input, h('datalist', { id: listId }, suggestions.map((id) => h('option', { value: id })))),
    read() {
      add();
      return values.length ? values : ['*'];
    },
  };
}

function keyForm({ key, defaults, suggestions }) {
  const editing = Boolean(key);
  const numberInput = (value, placeholder, step = 1) =>
    h('input', { class: 'input', type: 'number', min: 0, step, value: value ?? '', placeholder });
  const name = h('input', { class: 'input', maxLength: 200, value: key?.name ?? '', placeholder: '如 web-app、data-team' });
  const models = modelPicker(key && !(key.allowed_models.length === 1 && key.allowed_models[0] === '*') ? key.allowed_models : [], suggestions);
  const rpm = numberInput(key?.rpm, editing ? '不限' : `默认 ${defaults.rpm ?? '不限'}`);
  const concurrency = numberInput(key?.max_concurrency, editing ? '不限' : `默认 ${defaults.max_concurrency ?? '不限'}`);
  const budget = numberInput(key?.budget_usd, '不限', '0.01');
  const expires = h('input', { class: 'input', type: 'number', min: 1, step: 1, placeholder: editing ? '不修改' : '永不过期' });
  const neverExpire = h('input', { type: 'checkbox' });
  const note = h('input', { class: 'input', maxLength: 500, value: key?.note ?? '' });
  const enabled = h('input', { type: 'checkbox', checked: key ? key.enabled : true });
  const resetSpend = h('input', { type: 'checkbox' });

  const el = h('div', { class: 'form-grid' },
    field('名称', name, null, true),
    h('div', { class: 'field full' }, h('span', { class: 'label' }, '可用模型'), models.el,
      h('span', { class: 'hint' }, '支持网关别名、River 模型 ID 和通配符；留空表示全部模型。')),
    field('每分钟请求数', rpm, editing ? '留空或 0 表示不限' : '留空用默认值，0 表示不限'),
    field('最大并发', concurrency, editing ? '留空或 0 表示不限' : '留空用默认值，0 表示不限'),
    field('预算（USD）', budget, key ? `已用 ${fmt.usd(key.spent_usd)}；留空表示不限` : '留空表示不限'),
    field('有效期（天）', expires, editing ? `当前：${key.expires_at ? fmt.dateTime(key.expires_at) + ' 到期' : '永不过期'}` : '从现在起计算'),
    field('备注', note, null, true),
    editing ? h('div', { class: 'field full' },
      h('label', { class: 'check' }, enabled, '启用'),
      h('label', { class: 'check' }, neverExpire, '改为永不过期'),
      h('label', { class: 'check' }, resetSpend, `已用金额清零（当前 ${fmt.usd(key.spent_usd)}）`)) : null,
  );

  const number = (input) => (input.value === '' ? null : Number(input.value));
  return {
    el,
    read() {
      const payload = { name: name.value.trim(), allowed_models: models.read(), note: note.value.trim() || null };
      if (editing) {
        payload.rpm = number(rpm) ?? 0;
        payload.max_concurrency = number(concurrency) ?? 0;
        payload.budget_usd = number(budget);
        payload.enabled = enabled.checked;
        if (resetSpend.checked) payload.spent_usd = 0;
        if (neverExpire.checked) payload.expires_at = null;
        else if (expires.value) payload.expires_at = Date.now() / 1000 + Number(expires.value) * 86400;
      } else {
        if (rpm.value !== '') payload.rpm = number(rpm);
        if (concurrency.value !== '') payload.max_concurrency = number(concurrency);
        if (budget.value !== '') payload.budget_usd = number(budget);
        if (expires.value) payload.expires_in_days = number(expires);
      }
      return payload;
    },
  };
}

function showSecret({ key, secret }) {
  const snippet = `from openai import OpenAI\n\nclient = OpenAI(\n    api_key="${secret}",\n    base_url="${location.origin}/v1",\n)`;
  const modal = openModal({
    title: `密钥已创建：${key.name}`,
    wide: true,
    body: [
      h('p', { style: { marginTop: '0' } }, '这是该密钥唯一一次显示明文，请立即保存。网关只保存它的哈希值，之后无法再查看。'),
      h('div', { class: 'secret-box' }, h('input', { class: 'input', readOnly: true, value: secret, onFocus: (e) => e.target.select() }),
        h('button', { class: 'btn', type: 'button', onClick: () => copyText(secret) }, '复制')),
      h('div', { class: 'field', style: { marginTop: '16px' } }, h('span', { class: 'label' }, 'OpenAI SDK 用法'), h('pre', { class: 'code-block' }, snippet)),
    ],
    actions: [h('button', { class: 'btn btn-primary', type: 'button', onClick: () => modal.close() }, '我已保存')],
  });
}

export function render(container, ctx) {
  let keys = [];
  let suggestions = [];
  let query = '';
  const abort = new AbortController();
  const defaults = ctx.status.key_defaults || {};
  const tableBox = h('div', { class: 'card' });

  container.append(
    h('div', { class: 'page-header' },
      h('div', {}, h('h1', {}, '密钥管理'), h('div', { class: 'sub' }, '客户端用这些网关密钥调用 /v1；River 的 API key 只保存在网关服务器上。')),
      h('button', { class: 'btn btn-primary', type: 'button', onClick: () => openEditor() }, '＋ 新建密钥')),
    h('div', { class: 'toolbar' },
      h('input', { class: 'input', type: 'search', placeholder: '按名称、ID 或备注搜索', style: { maxWidth: '320px' }, onInput: (e) => { query = e.target.value.trim().toLowerCase(); draw(); } })),
    tableBox,
  );

  function openEditor(key = null) {
    const form = keyForm({ key, defaults, suggestions });
    const error = h('div', { class: 'error-text', style: { marginTop: '12px' } });
    const submit = h('button', { class: 'btn btn-primary', type: 'button' }, key ? '保存' : '创建');
    const modal = openModal({
      title: key ? `编辑密钥：${key.name}` : '新建网关密钥',
      wide: true,
      body: [form.el, error],
      actions: [h('button', { class: 'btn', type: 'button', onClick: () => modal.close() }, '取消'), submit],
    });
    submit.addEventListener('click', async () => {
      const payload = form.read();
      if (!payload.name) {
        error.textContent = '请填写名称。';
        return;
      }
      submit.disabled = true;
      try {
        if (key) {
          await admin(`/keys/${key.id}`, { method: 'PATCH', body: payload });
          toast('已保存', 'success');
          modal.close();
        } else {
          const created = await admin('/keys', { method: 'POST', body: payload });
          modal.close();
          showSecret(created);
        }
        load();
      } catch (e) {
        error.textContent = errorText(e);
      } finally {
        submit.disabled = false;
      }
    });
  }

  async function setEnabled(key, enabled) {
    try {
      await admin(`/keys/${key.id}`, { method: 'PATCH', body: { enabled } });
      toast(enabled ? `已启用 ${key.name}` : `已停用 ${key.name}`, 'success');
      load();
    } catch (e) {
      toast(errorText(e), 'error');
    }
  }

  async function remove(key) {
    const ok = await confirmDialog({
      title: `删除密钥 ${key.name}？`,
      message: '使用该密钥的应用会立即无法访问网关。已有的用量记录会保留。此操作不可撤销。',
      confirmText: '删除',
      danger: true,
    });
    if (!ok) return;
    try {
      await admin(`/keys/${key.id}`, { method: 'DELETE' });
      if (session.playgroundKey.startsWith(key.key_prefix)) session.playgroundKey = '';
      toast(`已删除 ${key.name}`, 'success');
      load();
    } catch (e) {
      toast(errorText(e), 'error');
    }
  }

  function row(key) {
    const state = keyState(key);
    const spend = key.budget_usd !== null
      ? h('div', { class: 'small' }, `${fmt.usd(key.spent_usd)} / ${fmt.usd(key.budget_usd)}`, meter(key.budget_usd > 0 ? key.spent_usd / key.budget_usd : 1))
      : h('div', { class: 'small' }, fmt.usd(key.spent_usd), h('span', { class: 'muted' }, ' · 不限'));
    const models = key.allowed_models;
    return h('tr', {},
      h('td', {},
        h('div', { class: 'cell-main' }, key.name),
        h('div', { class: 'cell-sub mono' }, `${key.key_prefix}… · ${key.id}`),
        key.note ? h('div', { class: 'cell-sub' }, key.note) : null),
      h('td', {}, badge(state.kind, state.icon, state.label)),
      h('td', {}, h('div', { class: 'chips' }, models.slice(0, 3).map((m) => h('span', { class: 'chip' }, h('span', { title: m }, m === '*' ? '* 全部' : m))),
        models.length > 3 ? h('span', { class: 'muted small', title: models.join(', ') }, `+${models.length - 3}`) : null)),
      h('td', { class: 'small nowrap' }, key.rpm ? `${fmt.int(key.rpm)} 次/分` : '不限速', h('div', { class: 'cell-sub' }, key.max_concurrency ? `并发 ${key.max_concurrency}` : '并发不限')),
      h('td', { style: { minWidth: '140px' } }, spend),
      h('td', { class: 'small nowrap' }, key.expires_at ? h('span', { title: fmt.dateTime(key.expires_at) }, fmt.date(key.expires_at)) : h('span', { class: 'muted' }, '永不')),
      h('td', { class: 'small nowrap' }, h('span', { title: key.last_used_at ? fmt.dateTime(key.last_used_at) : '' }, fmt.relative(key.last_used_at))),
      h('td', { class: 'nowrap' },
        h('a', { class: 'btn btn-sm btn-ghost', href: `#/logs?key=${encodeURIComponent(key.id)}` }, '日志'),
        h('button', { class: 'btn btn-sm btn-ghost', type: 'button', onClick: () => openEditor(key) }, '编辑'),
        h('button', { class: 'btn btn-sm btn-ghost', type: 'button', onClick: () => setEnabled(key, !key.enabled) }, key.enabled ? '停用' : '启用'),
        h('button', { class: 'btn btn-sm btn-ghost btn-danger', type: 'button', onClick: () => remove(key) }, '删除')),
    );
  }

  function draw() {
    const visible = keys.filter((key) => !query || [key.name, key.id, key.note || ''].some((v) => v.toLowerCase().includes(query)));
    clear(tableBox);
    if (!keys.length) {
      tableBox.append(emptyState('还没有网关密钥。点右上角“新建密钥”创建第一个。'));
      return;
    }
    if (!visible.length) {
      tableBox.append(emptyState('没有匹配的密钥。'));
      return;
    }
    tableBox.append(h('div', { class: 'table-wrap' }, h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, ['名称', '状态', '可用模型', '限额', '花费 / 预算', '到期', '最近使用', ''].map((t) => h('th', {}, t)))),
      h('tbody', {}, visible.map(row)))));
  }

  async function load() {
    tableBox.classList.add('is-loading');
    try {
      const [keyData, modelData] = await Promise.all([
        admin('/keys', { signal: abort.signal }),
        suggestions.length ? null : admin('/models', { signal: abort.signal }).catch(() => ({ data: [] })),
      ]);
      keys = keyData.data.sort((a, b) => b.created_at - a.created_at || a.name.localeCompare(b.name));
      if (modelData) {
        const ids = modelData.data.map((m) => m.id);
        const prefixes = [...new Set(ids.filter((id) => id.includes('/')).map((id) => id.split('/')[0] + '/*'))];
        suggestions = ['*', ...ids, ...prefixes];
      }
      draw();
    } catch (e) {
      if (e.name !== 'AbortError') clear(tableBox).append(h('div', { class: 'error-text' }, `加载失败：${errorText(e)}`));
    } finally {
      tableBox.classList.remove('is-loading');
    }
  }

  load();
  return () => abort.abort();
}
