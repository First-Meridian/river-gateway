// River 账号: the account pool. Each account is an API key created in River
// Console; one is in use at a time. River has no balance API, so the balance
// shown is the one recorded here minus what the gateway spent since.

import { admin, ApiError } from './api.js';
import { meter } from './charts.js';
import { badge, clear, confirmDialog, emptyState, errorText, fill, fmt, h, openModal, toast } from './dom.js';

const CONSOLE_KEYS_URL = 'https://console.river.ai/keys';

const ERRORS = {
  invalid_river_key: (e) => `River 拒绝了这个 key：${e.message.replace(/^River rejected this API key \(HTTP \d+\): /, '')}`,
  duplicate_account: () => '这个 key 已经在账号池里了。',
  upstream_unreachable: (e) => `连不上 River，暂时无法验证这个 key：${e.message}`,
  account_active: () => '不能删除正在使用的账号，请先切换到其他账号。',
  config_account: () => '配置文件中的 key 不能删除；要更换它，请修改 .env 里的 RIVER_API_KEY。',
  config_account_key: () => '这个账号的 key 来自配置文件；要更换它，请修改 .env 里的 RIVER_API_KEY 后重启网关。',
  account_has_no_key: () => '配置文件里没有 River key，不能切换到这个账号。',
};

const describeError = (error) => (error instanceof ApiError && ERRORS[error.code] ? ERRORS[error.code](error) : errorText(error));

function field(label, control, hint) {
  return h('div', { class: 'field' }, h('span', { class: 'label' }, label), control, hint ? h('span', { class: 'hint' }, hint) : null);
}

function checkLine(check) {
  if (!check) return h('div', { class: 'small muted' }, '连接：尚未检测');
  const when = `检测于 ${fmt.relative(check.at)}`;
  if (check.ok) return h('div', { class: 'small' }, badge('good', '✓', '连接正常'), ` ${check.models ?? '?'} 个模型 · ${when}`);
  return h('div', { class: 'small' }, badge('critical', '✕', check.status ? `HTTP ${check.status}` : '无法连接'), ` ${check.error || ''} · ${when}`);
}

function balanceBlock(account) {
  const b = account.balance;
  if (!b) {
    return h('div', { class: 'acct-balance' },
      h('div', { class: 'tile-label' }, '剩余额度（估算）'),
      h('div', { class: 'tile-value muted' }, '未录入'),
      h('div', { class: 'tile-sub' }, '点“记录余额”，填入 River Console 显示的当前余额。'));
  }
  const remaining = b.estimated_remaining_usd;
  return h('div', { class: 'acct-balance' },
    h('div', { class: 'tile-label' }, '剩余额度（估算）'),
    h('div', { class: 'tile-value' }, fmt.usd(remaining)),
    h('div', { class: 'tile-sub' },
      `录入 ${fmt.usd(b.recorded_usd)}（${fmt.dateTime(b.recorded_at).slice(5, 16)}），之后经网关消费 ${fmt.usd(b.spent_since_usd)}（${fmt.int(b.requests_since)} 次）`),
    meter(b.recorded_usd > 0 ? b.spent_since_usd / b.recorded_usd : 1));
}

export function render(container, ctx) {
  const abort = new AbortController();
  const list = h('div', { class: 'acct-grid' });
  let accounts = [];

  container.append(
    h('div', { class: 'page-header' },
      h('div', {}, h('h1', {}, 'River 账号'), h('div', { class: 'sub' }, '账号池：选择网关调用 River 时用哪个账号的 API key。切换后对下一个请求立即生效，无需重启。')),
      h('button', { class: 'btn btn-primary', type: 'button', onClick: () => openAdd() }, '＋ 添加账号')),
    h('div', { class: 'banner' },
      h('span', { class: 'banner-icon', style: { color: 'var(--accent)' }, 'aria-hidden': 'true' }, 'i'),
      h('div', {},
        h('div', {}, 'River 没有开放“用账号密码登录”和“查询余额”的接口，所以：'),
        h('div', {}, '· 每个账号用它在 River Console ', h('a', { href: CONSOLE_KEYS_URL, target: '_blank', rel: 'noopener noreferrer' }, 'API Keys 页面 ↗'), ' 创建的 key 接入；'),
        h('div', {}, '· 剩余额度 = 你从 Console 录入的余额 − 网关之后经该账号产生的费用（估算，不含网关之外的用量）；'),
        h('div', {}, '· River 返回 402（余额不足）时，账号会被自动标记，直到它的请求再次成功。'))),
    list,
  );

  async function load() {
    list.classList.add('is-loading');
    try {
      const result = await admin('/accounts', { signal: abort.signal });
      accounts = result.data;
      draw(result);
    } catch (e) {
      if (e.name === 'AbortError') return;
      fill(list, h('div', { class: 'card error-text' },
        e instanceof ApiError && e.code === 'accounts_disabled' ? '账号池未启用：配置里没有 default_upstream 或 accounts.upstream。' : `加载失败：${describeError(e)}`));
    } finally {
      list.classList.remove('is-loading');
    }
  }

  function draw(result) {
    clear(list);
    if (!accounts.length) {
      list.append(emptyState('还没有账号。'));
      return;
    }
    for (const account of accounts) list.append(card(account, result.upstream));
  }

  function card(account, upstream) {
    const actions = h('div', { class: 'acct-actions' },
      account.active ? null : h('button', { class: 'btn btn-sm btn-primary', type: 'button', onClick: () => activate(account) }, '设为当前使用'),
      h('button', { class: 'btn btn-sm', type: 'button', onClick: (e) => check(account, e.currentTarget) }, '测试连接'),
      h('button', { class: 'btn btn-sm', type: 'button', onClick: () => recordBalance(account) }, '记录余额'),
      h('button', { class: 'btn btn-sm btn-ghost', type: 'button', onClick: () => edit(account) }, '编辑'),
      account.source === 'config' ? null : h('button', { class: 'btn btn-sm btn-ghost btn-danger', type: 'button', onClick: () => remove(account) }, '删除'));
    return h('div', { class: 'card acct' + (account.active ? ' acct-active' : '') },
      h('div', { class: 'acct-head' },
        h('div', { class: 'acct-title' },
          h('span', { class: 'cell-main' }, account.name),
          account.active ? badge('good', '●', '使用中') : null,
          badge('neutral', account.source === 'config' ? '⚙' : '+', account.source === 'config' ? '配置文件' : '手动添加')),
        h('div', { class: 'cell-sub mono' }, account.has_key ? account.key_hint : '（没有 key）', ` · 上游 ${upstream}`),
        account.note ? h('div', { class: 'cell-sub' }, account.note) : null),
      balanceBlock(account),
      h('div', { class: 'acct-status' },
        account.low_balance
          ? h('div', { class: 'small' }, badge('critical', '!', 'River 报告余额不足'), ` ${fmt.relative(account.low_balance.at)} · ${account.low_balance.message || 'HTTP 402'}`)
          : null,
        checkLine(account.check),
        h('div', { class: 'small muted' }, `累计经网关：${fmt.int(account.usage.requests)} 次请求 · ${fmt.usd(account.usage.cost_usd)}`)),
      actions);
  }

  async function activate(account) {
    const ok = await confirmDialog({
      title: `切换到「${account.name}」？`,
      message: `之后的新请求都会使用这个账号的 River key（${account.key_hint || '无'}）。正在进行中的请求不受影响。`,
      confirmText: '切换',
    });
    if (!ok) return;
    try {
      await admin(`/accounts/${account.id}/activate`, { method: 'POST' });
      toast(`已切换到 ${account.name}`, 'success');
      await ctx.refreshStatus?.();
      load();
    } catch (e) {
      toast(describeError(e), 'error');
    }
  }

  async function check(account, button) {
    button.disabled = true;
    button.textContent = '检测中…';
    try {
      const { result } = await admin(`/accounts/${account.id}/check`, { method: 'POST' });
      toast(result.ok ? `${account.name}：连接正常，${result.models} 个模型` : `${account.name}：${result.error || '检测失败'}`, result.ok ? 'success' : 'error');
      load();
    } catch (e) {
      toast(describeError(e), 'error');
      button.disabled = false;
      button.textContent = '测试连接';
    }
  }

  function dialog({ title, fields, submitText, onSubmit }) {
    const error = h('div', { class: 'error-text', style: { marginTop: '12px' } });
    const submit = h('button', { class: 'btn btn-primary', type: 'button' }, submitText);
    const modal = openModal({
      title,
      body: [h('div', { class: 'stack' }, fields), error],
      actions: [h('button', { class: 'btn', type: 'button', onClick: () => modal.close() }, '取消'), submit],
    });
    submit.addEventListener('click', async () => {
      error.textContent = '';
      submit.disabled = true;
      try {
        await onSubmit();
        modal.close();
        load();
      } catch (e) {
        error.textContent = describeError(e);
      } finally {
        submit.disabled = false;
      }
    });
    return modal;
  }

  function openAdd() {
    const name = h('input', { class: 'input', maxLength: 100, placeholder: '如 个人账号、团队 B' });
    const key = h('input', { class: 'input', type: 'password', autocomplete: 'off', placeholder: 'rv_…' });
    const balance = h('input', { class: 'input', type: 'number', min: 0, step: '0.01', placeholder: '可选' });
    const note = h('input', { class: 'input', maxLength: 500 });
    dialog({
      title: '添加 River 账号',
      submitText: '验证并添加',
      fields: [
        field('名称', name),
        field('River API key', key, h('span', {}, '在该账号的 ', h('a', { href: CONSOLE_KEYS_URL, target: '_blank', rel: 'noopener noreferrer' }, 'River Console → API Keys ↗'), ' 创建。保存前会用它调一次 River 的 GET /models 验证（免费）。')),
        field('当前余额（USD）', balance, '可选：填 Console 显示的余额，用来估算剩余额度。'),
        field('备注', note),
      ],
      onSubmit: async () => {
        if (!name.value.trim() || !key.value.trim()) throw new Error('请填写名称和 API key。');
        await admin('/accounts', {
          method: 'POST',
          body: {
            name: name.value.trim(),
            api_key: key.value.trim(),
            note: note.value.trim() || null,
            balance_usd: balance.value === '' ? null : Number(balance.value),
          },
        });
        toast(`已添加 ${name.value.trim()}`, 'success');
      },
    });
  }

  function recordBalance(account) {
    const amount = h('input', { class: 'input', type: 'number', min: 0, step: '0.01', value: account.balance ? account.balance.estimated_remaining_usd.toFixed(2) : '' });
    dialog({
      title: `记录余额：${account.name}`,
      submitText: '保存',
      fields: [
        field('River Console 显示的当前余额（USD）', amount, '从现在起，网关经这个账号产生的费用会从这个数里扣减；“余额不足”标记也会清除。'),
        h('a', { href: 'https://console.river.ai/', target: '_blank', rel: 'noopener noreferrer', class: 'small' }, '打开 River Console 查看余额 ↗'),
      ],
      onSubmit: async () => {
        if (amount.value === '') throw new Error('请填写余额。');
        await admin(`/accounts/${account.id}`, { method: 'PATCH', body: { balance_usd: Number(amount.value) } });
        toast('已记录余额', 'success');
      },
    });
  }

  function edit(account) {
    const name = h('input', { class: 'input', maxLength: 100, value: account.name });
    const note = h('input', { class: 'input', maxLength: 500, value: account.note || '' });
    const key = h('input', { class: 'input', type: 'password', autocomplete: 'off', placeholder: '留空表示不更换' });
    dialog({
      title: `编辑账号：${account.name}`,
      submitText: '保存',
      fields: [
        field('名称', name),
        field('备注', note),
        account.source === 'config'
          ? h('div', { class: 'hint' }, '这个账号的 key 来自配置文件（.env 里的 RIVER_API_KEY），要更换请修改配置后重启网关。')
          : field('更换 API key', key, '换 key 后会清除之前的检测结果和余额不足标记。'),
      ],
      onSubmit: async () => {
        const body = { name: name.value.trim(), note: note.value.trim() || null };
        if (account.source !== 'config' && key.value.trim()) body.api_key = key.value.trim();
        await admin(`/accounts/${account.id}`, { method: 'PATCH', body });
        toast('已保存', 'success');
      },
    });
  }

  async function remove(account) {
    const ok = await confirmDialog({
      title: `从账号池删除「${account.name}」？`,
      message: '只会从网关里删除这个 key，不会影响 River 上的账号；历史用量记录会保留。',
      confirmText: '删除',
      danger: true,
    });
    if (!ok) return;
    try {
      await admin(`/accounts/${account.id}`, { method: 'DELETE' });
      toast(`已删除 ${account.name}`, 'success');
      load();
    } catch (e) {
      toast(describeError(e), 'error');
    }
  }

  load();
  return () => abort.abort();
}
