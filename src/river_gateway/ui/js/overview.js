// 总览: KPI tiles, a trend chart and breakdowns by model and key, all scoped
// by one filter row (time range + metric).

import { admin, tzOffset } from './api.js';
import { barList, columnChart, dataTable } from './charts.js';
import { clear, dayKey, emptyState, errorText, fmt, h, hourKey, toast } from './dom.js';

const RANGES = [
  { id: 'today', label: '今天' },
  { id: '7d', label: '近 7 天' },
  { id: '30d', label: '近 30 天' },
];

const METRICS = {
  requests: { label: '请求数', value: (r) => r.requests, format: fmt.int, axis: fmt.compact, integer: true },
  cost: { label: '费用', value: (r) => r.cost_usd, format: fmt.usd, axis: fmt.usdAxis, integer: false },
  tokens: { label: 'Token', value: (r) => r.prompt_tokens + r.completion_tokens, format: fmt.int, axis: fmt.compact, integer: true },
};

function rangeWindow(range) {
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  const buckets = [];
  if (range === 'today') {
    const now = new Date();
    for (let hour = 0; hour <= now.getHours(); hour++) {
      const d = new Date(start);
      d.setHours(hour);
      buckets.push({ key: hourKey(d), label: `${String(hour).padStart(2, '0')}:00`, tooltipLabel: `今天 ${String(hour).padStart(2, '0')}:00` });
    }
    return { since: start.getTime() / 1000, groupBy: 'hour', buckets };
  }
  const days = range === '7d' ? 7 : 30;
  start.setDate(start.getDate() - (days - 1));
  for (let i = 0; i < days; i++) {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    const key = dayKey(d);
    buckets.push({ key, label: key.slice(5), tooltipLabel: key });
  }
  return { since: start.getTime() / 1000, groupBy: 'day', buckets };
}

function tile(label, value, sub) {
  return h('div', { class: 'tile' }, h('div', { class: 'tile-label' }, label), h('div', { class: 'tile-value' }, value), h('div', { class: 'tile-sub' }, sub || ' '));
}

function segmented(options, current, onChange) {
  const group = h('div', { class: 'seg', role: 'group' });
  for (const option of options) {
    group.append(
      h('button', {
        type: 'button',
        'aria-pressed': String(option.id === current()),
        onClick: () => {
          for (const button of group.children) button.setAttribute('aria-pressed', 'false');
          group.children[options.indexOf(option)].setAttribute('aria-pressed', 'true');
          onChange(option.id);
        },
      }, option.label),
    );
  }
  return group;
}

export function render(container, ctx) {
  let range = sessionStorage.getItem('rgw.range') || '7d';
  let metric = sessionStorage.getItem('rgw.metric') || 'requests';
  let data = null;
  let showTable = false;
  const abort = new AbortController();

  const updated = h('span', { class: 'muted small' });
  const kpis = h('div', { class: 'kpis' });
  const chartBox = h('div', { class: 'chart' });
  const chartTitle = h('h2');
  const tableToggle = h('button', { class: 'btn btn-sm', type: 'button', onClick: () => { showTable = !showTable; drawTrend(); } }, '表格视图');
  const modelsBox = h('div', { class: 'barlist' });
  const modelsTitle = h('h2');
  const keysBox = h('div');
  const body = h('div', {}, kpis,
    h('div', { class: 'card', style: { marginTop: '16px' } }, h('div', { class: 'card-head' }, chartTitle, tableToggle), chartBox),
    h('div', { class: 'grid-2' },
      h('div', { class: 'card' }, h('div', { class: 'card-head' }, modelsTitle, h('span', { class: 'sub' }, '按实际调用的模型（回退后的模型）')), modelsBox),
      h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, '按密钥'), h('span', { class: 'sub' }, '按费用排序')), keysBox)));

  container.append(
    h('div', { class: 'page-header' },
      h('div', {}, h('h1', {}, '总览'), h('div', { class: 'sub' }, '经过网关的请求、Token 与估算费用（按 River 公开价格）')),
    ),
    h('div', { class: 'toolbar' },
      segmented(RANGES, () => range, (id) => { range = id; sessionStorage.setItem('rgw.range', id); load(); }),
      segmented(Object.entries(METRICS).map(([id, m]) => ({ id, label: m.label })), () => metric, (id) => { metric = id; sessionStorage.setItem('rgw.metric', id); draw(); }),
      h('span', { class: 'spacer' }),
      updated,
      h('button', { class: 'btn btn-sm', type: 'button', onClick: () => load() }, '刷新'),
    ),
    body,
  );

  function drawTrend() {
    const m = METRICS[metric];
    const { buckets } = data.window;
    const byKey = new Map(data.series.map((row) => [row[data.window.groupBy], row]));
    const points = buckets.map((b) => ({ ...b, value: byKey.has(b.key) ? m.value(byKey.get(b.key)) : 0 }));
    chartTitle.textContent = `${m.label}趋势（${data.window.groupBy === 'hour' ? '按小时' : '按天'}）`;
    tableToggle.textContent = showTable ? '图表视图' : '表格视图';
    if (!data.totals.requests) {
      clear(chartBox).append(emptyState('所选时间段内还没有请求。'));
      return;
    }
    if (showTable) {
      clear(chartBox).append(dataTable(
        [{ title: data.window.groupBy === 'hour' ? '时间' : '日期', value: (p) => p.tooltipLabel }, { title: m.label, num: true, value: (p) => m.format(p.value) }],
        points,
      ));
      return;
    }
    columnChart(chartBox, points, { format: m.format, axisFormat: m.axis, integer: m.integer, ariaLabel: chartTitle.textContent });
  }

  function draw() {
    const m = METRICS[metric];
    const t = data.totals;
    const avgLatency = data.series.reduce((sum, row) => sum + (row.avg_latency_ms || 0) * row.requests, 0) / (t.requests || 1);
    clear(kpis).append(
      tile('请求数', fmt.compact(t.requests), `成功 ${fmt.int(t.succeeded)}`),
      tile('成功率', t.requests ? fmt.pct(t.succeeded / t.requests) : '–', t.requests ? `失败 ${fmt.int(t.requests - t.succeeded)}` : ''),
      tile('输入 Token', fmt.compact(t.prompt_tokens), `其中缓存 ${fmt.compact(t.cached_tokens)}`),
      tile('输出 Token', fmt.compact(t.completion_tokens), `其中推理 ${fmt.compact(t.reasoning_tokens)}`),
      tile('估算费用', fmt.usd(t.cost_usd), 'USD'),
      tile('平均耗时', t.requests ? fmt.ms(avgLatency) : '–', '含完整流式输出'),
    );
    drawTrend();

    modelsTitle.textContent = `按模型的${m.label}`;
    if (!data.byModel.length) clear(modelsBox).append(emptyState('暂无数据'));
    else {
      const rows = data.byModel
        .map((row) => ({ name: row.target || '（未路由）', value: m.value(row), detail: `${fmt.int(row.requests)} 次请求 · ${fmt.usd(row.cost_usd)} · ${fmt.compact(row.prompt_tokens + row.completion_tokens)} Token` }))
        .sort((a, b) => b.value - a.value)
        .slice(0, 10);
      barList(modelsBox, rows, { format: m.format });
    }

    clear(keysBox).append(
      data.byKey.length
        ? dataTable(
            [
              { title: '密钥', value: (r) => h('span', { title: r.key || '' }, r.key_name || (r.key ? `${r.key}（已删除）` : '–')) },
              { title: '请求', num: true, value: (r) => fmt.int(r.requests) },
              { title: '成功率', num: true, value: (r) => fmt.pct(r.succeeded / r.requests) },
              { title: 'Token', num: true, value: (r) => fmt.compact(r.prompt_tokens + r.completion_tokens) },
              { title: '费用', num: true, value: (r) => fmt.usd(r.cost_usd) },
            ],
            data.byKey.slice(0, 10),
          )
        : emptyState('暂无数据'),
    );
  }

  async function load() {
    const win = rangeWindow(range);
    body.classList.add('is-loading');
    const params = { since: win.since, tz_offset: tzOffset() };
    try {
      const [series, byModel, byKey] = await Promise.all([
        admin('/usage', { params: { ...params, group_by: win.groupBy }, signal: abort.signal }),
        admin('/usage', { params: { ...params, group_by: 'target' }, signal: abort.signal }),
        admin('/usage', { params: { ...params, group_by: 'key' }, signal: abort.signal }),
      ]);
      data = { window: win, series: series.data, totals: series.totals, byModel: byModel.data, byKey: byKey.data };
      draw();
      updated.textContent = `更新于 ${fmt.shortTime(Date.now() / 1000)}`;
    } catch (error) {
      if (error.name !== 'AbortError') toast(`加载用量失败：${errorText(error)}`, 'error');
    } finally {
      body.classList.remove('is-loading');
    }
  }

  let lastWidth = 0;
  const resize = new ResizeObserver((entries) => {
    const width = Math.floor(entries[0].contentRect.width);
    if (width === lastWidth) return;
    lastWidth = width;
    if (data && !showTable && data.totals.requests) drawTrend();
  });
  resize.observe(chartBox);
  load();
  return () => {
    abort.abort();
    resize.disconnect();
  };
}
