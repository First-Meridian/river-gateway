// 状态: gateway, upstream connectivity, model routes, prices and links.

import { admin } from './api.js';
import { dataTable } from './charts.js';
import { badge, clear, emptyState, errorText, fill, fmt, h, toast } from './dom.js';

export const WARNING_TEXT = {
  weak_admin_token: () => '管理令牌是占位值或少于 16 个字符，能访问网关的人都可能拿它管理密钥。请把 GATEWAY_ADMIN_TOKEN 改成足够长的随机字符串，然后重启网关。',
  missing_upstream_key: (w) => `上游 ${w.upstream} 没有配置 api_key，River 会拒绝发往它的请求。`,
  pricing_unavailable: (w) => `无法获取 River 价格表（${w.message}），费用暂时无法计算。`,
  account_low_balance: (w) => `当前 River 账号「${w.account}」余额不足（River 返回 402：${w.message}）。请到 River Console 充值，或在“River 账号”页切换到其他账号。`,
};

export function warningText(warning) {
  return WARNING_TEXT[warning.code]?.(warning) || warning.message;
}

export function render(container, ctx) {
  const abort = new AbortController();
  const gatewayBox = h('div');
  const upstreamBox = h('div');
  const routesBox = h('div');
  const pricingBox = h('div');
  const pricingMeta = h('span', { class: 'sub' });
  const checkButton = h('button', { class: 'btn btn-sm', type: 'button', onClick: () => checkUpstreams() }, '测试连接');
  const refreshButton = h('button', { class: 'btn btn-sm', type: 'button', onClick: () => refreshPricing() }, '立即刷新');
  let status = ctx.status;
  let checks = {};

  container.append(
    h('div', { class: 'page-header' }, h('div', {}, h('h1', {}, '状态'), h('div', { class: 'sub' }, '网关配置、上游连接、模型路由与价格表'))),
    h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, '网关')), gatewayBox),
    h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, '上游'), checkButton), upstreamBox),
    h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, '模型路由')), routesBox),
    h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, '价格表'), pricingMeta), refreshButton), pricingBox),
    h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, '其他入口')),
      h('div', { class: 'toolbar', style: { marginBottom: '0' } },
        h('a', { class: 'btn', href: '/docs', target: '_blank', rel: 'noopener' }, '接口文档（Swagger）↗'),
        h('a', { class: 'btn', href: '/metrics', target: '_blank', rel: 'noopener' }, 'Prometheus 指标 ↗'),
        h('a', { class: 'btn', href: '/healthz', target: '_blank', rel: 'noopener' }, '健康检查 ↗'))),
  );

  function drawGateway() {
    const items = [
      ['版本', status.version],
      ['已运行', fmt.duration(status.uptime_seconds)],
      ['网关密钥', `${fmt.int(status.keys)} 个`],
      ['进行中的请求', fmt.int(status.inflight)],
      ['默认上游', status.default_upstream || '无（只允许已配置的路由）'],
      ['当前 River 账号', status.account ? h('a', { href: '#/accounts' }, status.account.name) : '–'],
      ['新密钥默认限额', `${status.key_defaults?.rpm ? `${status.key_defaults.rpm} 次/分` : '不限速'} · ${status.key_defaults?.max_concurrency ? `并发 ${status.key_defaults.max_concurrency}` : '并发不限'}`],
    ];
    fill(gatewayBox,
      h('dl', { class: 'kv' }, items.flatMap(([label, value]) => [h('dt', {}, label), h('dd', {}, value)])),
      status.warnings?.length
        ? h('div', { style: { marginTop: '14px' } }, status.warnings.map((w) =>
          h('div', { class: 'banner', style: { marginBottom: '8px' } }, h('span', { class: 'banner-icon', 'aria-hidden': 'true' }, '!'), h('span', {}, warningText(w)))))
        : null,
    );
  }

  function checkCell(name) {
    const result = checks[name];
    if (result === undefined) return h('span', { class: 'muted' }, '–');
    if (result === 'pending') return h('span', { class: 'muted' }, '检测中…');
    if (result.ok) {
      return h('div', {}, badge('good', '✓', `连接正常 · ${fmt.ms(result.latency_ms)}`),
        result.models != null ? h('div', { class: 'cell-sub' }, `${result.models} 个可用模型`) : null);
    }
    return h('div', {}, badge('critical', '✕', result.status ? `HTTP ${result.status}` : '无法连接'),
      h('div', { class: 'cell-sub', style: { maxWidth: '360px', whiteSpace: 'normal' } }, result.error || ''));
  }

  function drawUpstreams() {
    const rows = Object.entries(status.upstreams).map(([name, u]) => ({ name, ...u }));
    clear(upstreamBox).append(dataTable([
      { title: '名称', value: (r) => h('span', {}, r.name, r.name === status.default_upstream ? h('span', { class: 'muted small' }, '（默认）') : null) },
      { title: '地址', value: (r) => h('span', { class: 'mono' }, r.base_url) },
      { title: 'API key', value: (r) => (r.api_key_set ? badge('good', '✓', '已配置') : badge('critical', '✕', '未配置')) },
      { title: '最大并发', value: (r) => (r.max_concurrency ? String(r.max_concurrency) : '不限') },
      { title: '连接测试', value: (r) => checkCell(r.name) },
    ], rows));
  }

  function drawRoutes() {
    clear(routesBox).append(status.models.length
      ? dataTable([
        { title: '名称', value: (r) => h('span', { class: 'mono' }, r.name) },
        { title: '实际模型', value: (r) => h('span', { class: 'mono' }, r.target) },
        { title: '上游', value: (r) => r.upstream },
        { title: '回退', value: (r) => (r.fallbacks.length ? h('span', { class: 'mono' }, r.fallbacks.join(' → ')) : h('span', { class: 'muted' }, '无')) },
      ], status.models)
      : emptyState(status.default_upstream
        ? `没有配置路由：所有模型名都会原样转发到上游 ${status.default_upstream}。`
        : '没有配置路由。'));
  }

  function drawPricing(pricing) {
    pricingMeta.textContent = pricing.last_refresh
      ? `美元 / 百万 Token · 更新于 ${fmt.dateTime(pricing.last_refresh)}`
      : '美元 / 百万 Token · 尚未成功获取';
    const rows = Object.entries(pricing.models).map(([model, p]) => ({ model, ...p }));
    fill(pricingBox,
      pricing.last_error ? h('div', { class: 'error-text', style: { marginBottom: '8px' } }, `最近一次刷新失败：${pricing.last_error}`) : null,
      rows.length
        ? dataTable([
          { title: '模型', value: (r) => h('span', { class: 'mono' }, r.model) },
          { title: '输入', num: true, value: (r) => `$${r.input}` },
          { title: '缓存输入', num: true, value: (r) => (r.cached_input == null ? '同输入' : `$${r.cached_input}`) },
          { title: '输出', num: true, value: (r) => `$${r.output}` },
        ], rows)
        : emptyState('价格表为空。'),
      pricing.source ? h('div', { class: 'muted small', style: { marginTop: '8px' } }, `来源：${pricing.source}`) : null,
    );
  }

  async function checkUpstreams() {
    checkButton.disabled = true;
    for (const name of Object.keys(status.upstreams)) checks[name] = 'pending';
    drawUpstreams();
    try {
      checks = (await admin('/upstreams/check', { signal: abort.signal })).data;
    } catch (e) {
      if (e.name === 'AbortError') return;
      checks = {};
      toast(`连接测试失败：${errorText(e)}`, 'error');
    } finally {
      checkButton.disabled = false;
    }
    drawUpstreams();
  }

  async function refreshPricing() {
    refreshButton.disabled = true;
    try {
      const result = await admin('/pricing/refresh', { method: 'POST', signal: abort.signal });
      drawPricing(result);
      toast(result.refreshed ? '价格表已刷新' : '刷新失败，继续使用上次的价格', result.refreshed ? 'success' : 'error');
    } catch (e) {
      if (e.name !== 'AbortError') toast(errorText(e), 'error');
    } finally {
      refreshButton.disabled = false;
    }
  }

  async function load() {
    try {
      const [fresh, pricing] = await Promise.all([
        admin('/status', { signal: abort.signal }),
        admin('/pricing', { signal: abort.signal }),
      ]);
      status = fresh;
      ctx.status = fresh;
      drawGateway();
      drawUpstreams();
      drawRoutes();
      drawPricing(pricing);
      checkUpstreams();
    } catch (e) {
      if (e.name !== 'AbortError') toast(`加载状态失败：${errorText(e)}`, 'error');
    }
  }

  drawGateway();
  drawUpstreams();
  drawRoutes();
  load();
  return () => abort.abort();
}
