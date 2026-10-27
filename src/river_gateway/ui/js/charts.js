// Charts built to the dataviz reference: one series in slot-1 blue, thin marks
// (<= 24px bars, 4px rounded data end, square at the baseline, >= 2px gap),
// hairline solid grid, per-bar hover/focus tooltip with a larger hit area,
// selective direct label (the maximum only) and a table-view twin.

import { clear, h, s } from './dom.js';

function niceTicks(max, count, integer) {
  if (!(max > 0)) return [0, 1];
  const raw = max / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / magnitude;
  let step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * magnitude;
  if (integer) step = Math.max(1, Math.round(step));
  const ticks = [];
  for (let i = 0; ; i++) {
    const value = Number((i * step).toPrecision(12));
    ticks.push(value);
    if (value >= max) break;
  }
  return ticks;
}

function barPath(x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, height);
  return `M${x},${y + height}V${y + r}A${r},${r} 0 0 1 ${x + r},${y}H${x + width - r}A${r},${r} 0 0 1 ${x + width},${y + r}V${y + height}Z`;
}

/**
 * Column chart for a time series.
 * points: [{label, tooltipLabel, value}], options: {format, axisFormat, integer, ariaLabel}
 */
export function columnChart(container, points, { format, axisFormat = format, integer = false, ariaLabel = '' }) {
  clear(container);
  const width = Math.max(320, Math.floor(container.clientWidth || 640));
  const height = 236; // includes the x-axis band, so the card never scrolls
  const margin = { top: 22, right: 8, bottom: 26, left: 56 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const values = points.map((p) => p.value || 0);
  const max = Math.max(0, ...values);
  const ticks = niceTicks(max, 4, integer);
  const top = ticks[ticks.length - 1] || 1;
  const band = plotWidth / Math.max(points.length, 1);
  const barWidth = Math.max(2, Math.min(24, band - 2));
  const y = (value) => margin.top + plotHeight - (value / top) * plotHeight;

  const svg = s('svg', { viewBox: `0 0 ${width} ${height}`, height, role: 'img', 'aria-label': ariaLabel });
  for (const tick of ticks) {
    const ty = Math.round(y(tick)) + 0.5;
    if (tick > 0) svg.append(s('line', { class: 'gridline', x1: margin.left, x2: width - margin.right, y1: ty, y2: ty }));
    svg.append(s('text', { class: 'tick', x: margin.left - 8, y: ty + 4, 'text-anchor': 'end' }, axisFormat(tick)));
  }
  const baseline = Math.round(y(0)) + 0.5;
  svg.append(s('line', { class: 'baseline', x1: margin.left, x2: width - margin.right, y1: baseline, y2: baseline }));

  const labelEvery = Math.max(1, Math.ceil(points.length / Math.max(1, Math.floor(plotWidth / 56))));
  const maxIndex = max > 0 ? values.indexOf(max) : -1;
  const tooltip = h('div', { class: 'tooltip hidden', role: 'tooltip' });

  points.forEach((point, i) => {
    const x = margin.left + i * band + (band - barWidth) / 2;
    const value = point.value || 0;
    const barTop = y(value);
    const hit = s('rect', {
      class: 'hit',
      x: margin.left + i * band,
      y: margin.top,
      width: band,
      height: plotHeight,
      tabindex: 0,
      'aria-label': `${point.tooltipLabel || point.label}: ${format(value)}`,
    });
    const show = () => {
      tooltip.replaceChildren(h('strong', {}, format(value)), h('span', {}, point.tooltipLabel || point.label));
      tooltip.classList.remove('hidden');
      const left = Math.min(Math.max(x + barWidth / 2 - tooltip.offsetWidth / 2, 0), width - tooltip.offsetWidth);
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${Math.max(0, barTop - tooltip.offsetHeight - 8)}px`;
    };
    const hide = () => tooltip.classList.add('hidden');
    hit.addEventListener('pointerenter', show);
    hit.addEventListener('pointerleave', hide);
    hit.addEventListener('focus', show);
    hit.addEventListener('blur', hide);
    svg.append(hit);
    if (value > 0) svg.append(s('path', { class: 'bar', d: barPath(x, barTop, barWidth, y(0) - barTop, 4) }));
    else svg.append(s('g'));
    if (i % labelEvery === 0) {
      svg.append(s('text', { class: 'tick', x: x + barWidth / 2, y: height - 8, 'text-anchor': 'middle' }, point.label));
    }
    if (i === maxIndex) {
      svg.append(s('text', { class: 'value-label', x: x + barWidth / 2, y: barTop - 6, 'text-anchor': 'middle' }, format(value)));
    }
  });
  container.append(svg, tooltip);
}

/** Horizontal bars for a ranking: rows [{name, value, detail}]. */
export function barList(container, rows, { format }) {
  clear(container);
  const max = Math.max(0, ...rows.map((row) => row.value || 0));
  for (const row of rows) {
    const bar = h('div', { class: 'barlist-bar' });
    bar.style.width = `${max > 0 ? Math.max(0.5, ((row.value || 0) / max) * 100) : 0}%`;
    container.append(
      h(
        'div',
        { class: 'barlist-row' },
        h('div', { class: 'barlist-name', title: row.name }, row.name),
        h('div', { class: 'barlist-value' }, format(row.value || 0)),
        h('div', { class: 'barlist-track' }, bar),
        row.detail ? h('div', { class: 'barlist-detail' }, row.detail) : null,
      ),
    );
  }
}

/** Table twin of a chart. */
export function dataTable(columns, rows) {
  return h(
    'div',
    { class: 'table-wrap' },
    h(
      'table',
      { class: 'table' },
      h('thead', {}, h('tr', {}, columns.map((c) => h('th', { class: c.num ? 'num' : '' }, c.title)))),
      h('tbody', {}, rows.map((row) => h('tr', {}, columns.map((c) => h('td', { class: c.num ? 'num' : '' }, c.value(row)))))),
    ),
  );
}

/** Budget meter: fill turns warning at 80% and critical at 100%. */
export function meter(ratio) {
  const fill = h('div', { class: 'meter-fill' + (ratio >= 1 ? ' critical' : ratio >= 0.8 ? ' warning' : '') });
  fill.style.width = `${Math.min(100, Math.max(0, ratio * 100))}%`;
  return h('div', { class: 'meter', role: 'meter', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': Math.round(ratio * 100) }, fill);
}
