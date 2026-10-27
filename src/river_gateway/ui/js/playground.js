// Playground: chat through the gateway with a gateway key, exactly like a
// client would (auth, limits, routing, fallbacks and billing all apply).

import { admin, ApiError, session } from './api.js';
import { clear, copyText, errorText, fmt, h, toast } from './dom.js';

const SETTINGS_KEY = 'rgw.playground.settings';

/** Parse a fetch() SSE body into JSON events ('[DONE]' passes through as a string). */
async function* sseEvents(response) {
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      let match;
      while ((match = /\r?\n\r?\n/.exec(buffer))) {
        const raw = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        const data = raw
          .split(/\r?\n/)
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).replace(/^ /, ''))
          .join('\n');
        if (!data) continue;
        if (data === '[DONE]') {
          yield '[DONE]';
          continue;
        }
        try {
          yield JSON.parse(data);
        } catch {
          // ignore a malformed event
        }
      }
    }
  } finally {
    reader.cancel().catch(() => {});
  }
}

function estimateCost(price, usage) {
  if (!price || !usage) return null;
  const prompt = usage.prompt_tokens || 0;
  const cached = Math.min(usage.prompt_tokens_details?.cached_tokens || 0, prompt);
  const cachedRate = price.cached_input ?? price.input;
  return ((prompt - cached) * price.input + cached * cachedRate + (usage.completion_tokens || 0) * price.output) / 1e6;
}

function userBubble(text) {
  return h('div', { class: 'msg msg-user' }, h('div', { class: 'msg-role' }, '你'), h('div', { class: 'msg-bubble' }, text));
}

function assistantBubble() {
  const reasoningText = h('div');
  const reasoning = h('details', { class: 'reasoning hidden' }, h('summary', {}, '思考过程'), reasoningText);
  const bubble = h('div', { class: 'msg-bubble cursor' });
  const meta = h('div', { class: 'msg-meta' }, h('span', {}, '等待响应…'));
  const el = h('div', { class: 'msg msg-assistant' }, h('div', { class: 'msg-role' }, '助手'), reasoning, bubble, meta);

  const metaItems = (info) => {
    const items = [];
    if (info.model) items.push(h('span', { class: 'mono' }, `${info.model} @ ${info.upstream}`));
    if (info.attempts && info.attempts !== '1') items.push(h('span', {}, `尝试 ${info.attempts} 次`));
    if (info.firstToken != null) items.push(h('span', {}, `首字 ${fmt.ms(info.firstToken)}`));
    if (info.total != null) items.push(h('span', {}, `总耗时 ${fmt.ms(info.total)}`));
    if (info.usage) {
      const reasoningTokens = info.usage.completion_tokens_details?.reasoning_tokens;
      items.push(h('span', {}, `Token ${fmt.int(info.usage.prompt_tokens)} → ${fmt.int(info.usage.completion_tokens)}${reasoningTokens ? `（推理 ${fmt.int(reasoningTokens)}）` : ''}`));
    }
    if (info.cost != null) items.push(h('span', {}, `≈ ${fmt.usd(info.cost)}`));
    if (info.finish && info.finish !== 'stop') items.push(h('span', {}, `finish_reason: ${info.finish}`));
    if (info.stopped) items.push(h('span', {}, '已手动停止'));
    if (info.requestId) {
      items.push(h('button', { class: 'btn btn-sm btn-ghost', type: 'button', title: '复制网关请求 ID', onClick: () => copyText(info.requestId) }, info.requestId));
    }
    return items;
  };

  return {
    el,
    setReasoning(text, live = false) {
      if (!text) return;
      reasoning.classList.remove('hidden');
      if (live) reasoning.open = true;
      reasoningText.textContent = text;
    },
    setContent(text) {
      bubble.textContent = text;
    },
    finish(info) {
      bubble.classList.remove('cursor');
      reasoning.open = false;
      if (!bubble.textContent) bubble.textContent = info.stopped ? '（已停止，没有输出）' : '（没有文本输出）';
      clear(meta).append(...metaItems(info));
    },
    fail(error, info) {
      el.classList.add('msg-error');
      bubble.classList.remove('cursor');
      bubble.textContent = `${error.status ? `HTTP ${error.status}：` : ''}${error.message}`;
      clear(meta).append(...metaItems(info), h('span', {}, '这条消息不会计入后续对话上下文'));
    },
  };
}

export function render(container) {
  const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
  const history = [];
  let controller = null;
  let prices = {};
  const abort = new AbortController();

  const keyBox = h('div', { class: 'stack-sm' });
  const modelSelect = h('select', { class: 'select' }, h('option', { value: '' }, '加载中…'));
  const streamBox = h('input', { type: 'checkbox', checked: saved.stream ?? true });
  const maxTokens = h('input', { class: 'input', type: 'number', min: 1, step: 1, value: saved.maxTokens ?? 1024 });
  const temperature = h('input', { class: 'input', type: 'number', min: 0, max: 2, step: '0.1', value: saved.temperature ?? '', placeholder: '默认' });
  const effort = h('select', { class: 'select' },
    ['', 'none', 'low', 'high', 'max'].map((v) => h('option', { value: v, selected: v === (saved.effort ?? '') }, v || '默认（不传）')));
  const system = h('textarea', { class: 'textarea', rows: 3, placeholder: '可选' }, saved.system ?? '');
  const messages = h('div', { class: 'pg-messages' });
  const input = h('textarea', { class: 'textarea', rows: 3, placeholder: '输入消息，Ctrl+Enter 发送' });
  const sendButton = h('button', { class: 'btn btn-primary', type: 'button', onClick: () => send() }, '发送');
  const stopButton = h('button', { class: 'btn hidden', type: 'button', onClick: () => controller?.abort() }, '停止');

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      send();
    }
  });

  const field = (label, control, hint) =>
    h('div', { class: 'field' }, h('span', { class: 'label' }, label), control, hint ? h('span', { class: 'hint' }, hint) : null);

  container.append(
    h('div', { class: 'page-header' },
      h('div', {}, h('h1', {}, 'Playground'), h('div', { class: 'sub' }, '通过网关调用 Chat Completions，和真实客户端走同一条路径：鉴权、限流、路由、回退和计费都会生效。'))),
    h('div', { class: 'playground' },
      h('div', { class: 'card pg-settings' },
        field('网关密钥', keyBox),
        field('模型', modelSelect, '网关别名会按路由转到实际模型'),
        h('label', { class: 'check' }, streamBox, '流式输出'),
        field('最大输出 Token（max_tokens）', maxTokens, '推理 token 也计入这个上限'),
        field('温度（temperature）', temperature, '0–2，留空不传'),
        field('推理强度（reasoning_effort）', effort, 'DeepSeek 支持 none/high/max，GLM 支持 low/high/max'),
        field('系统提示词', system),
        h('button', { class: 'btn btn-ghost', type: 'button', onClick: () => reset() }, '清空对话')),
      h('div', { class: 'card pg-chat' },
        messages,
        h('div', { class: 'pg-composer' }, input,
          h('div', { class: 'pg-composer-actions' }, h('span', { class: 'muted small' }, '对话历史只保存在当前页面'), h('span', { class: 'spacer', style: { flex: '1' } }), stopButton, sendButton)))),
  );

  function drawKey() {
    clear(keyBox);
    const key = session.playgroundKey;
    if (key) {
      keyBox.append(h('div', { class: 'small' }, '使用 ', h('span', { class: 'mono' }, `${key.slice(0, 10)}…`)),
        h('button', { class: 'btn btn-sm btn-ghost', type: 'button', onClick: () => { session.playgroundKey = ''; drawKey(); } }, '更换密钥'));
      return;
    }
    const paste = h('input', { class: 'input', type: 'password', placeholder: '或粘贴已有的 rgw_… 密钥', autocomplete: 'off' });
    keyBox.append(
      h('button', { class: 'btn btn-sm btn-primary', type: 'button', onClick: createKey }, '创建 Playground 密钥'),
      h('div', { class: 'secret-box', style: { marginTop: '6px' } }, paste,
        h('button', { class: 'btn btn-sm', type: 'button', onClick: () => { if (paste.value.trim()) { session.playgroundKey = paste.value.trim(); drawKey(); } } }, '使用')),
      h('span', { class: 'hint' }, '自动创建的密钥名为 playground，7 天后过期，只保存在本浏览器。'));
  }

  async function createKey() {
    try {
      const created = await admin('/keys', {
        method: 'POST',
        body: { name: 'playground', note: '由管理控制台创建，供 Playground 使用', expires_in_days: 7 },
      });
      session.playgroundKey = created.secret;
      toast('已创建 Playground 密钥（7 天后过期）', 'success');
      drawKey();
    } catch (e) {
      toast(`创建密钥失败：${errorText(e)}`, 'error');
    }
  }

  function saveSettings() {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({
      model: modelSelect.value,
      stream: streamBox.checked,
      maxTokens: maxTokens.value,
      temperature: temperature.value,
      effort: effort.value,
      system: system.value,
    }));
  }

  function reset() {
    if (controller) controller.abort();
    history.length = 0;
    clear(messages).append(h('div', { class: 'empty' }, '发一条消息试试。回复里会显示实际使用的模型、首字时间、Token 和估算费用。'));
  }

  function setBusy(busy) {
    sendButton.disabled = busy;
    stopButton.classList.toggle('hidden', !busy);
  }

  const scrollDown = () => {
    messages.scrollTop = messages.scrollHeight;
  };

  async function send() {
    const text = input.value.trim();
    if (!text || controller) return;
    const key = session.playgroundKey;
    if (!key) {
      toast('请先创建或填写一个网关密钥', 'error');
      return;
    }
    if (!modelSelect.value) {
      toast('请选择模型', 'error');
      return;
    }
    saveSettings();
    if (!history.length) clear(messages);
    history.push({ role: 'user', content: text });
    input.value = '';
    messages.append(userBubble(text));
    const view = assistantBubble();
    messages.append(view.el);
    scrollDown();

    const body = {
      model: modelSelect.value,
      messages: [...(system.value.trim() ? [{ role: 'system', content: system.value.trim() }] : []), ...history],
      max_tokens: Number(maxTokens.value) || 1024,
    };
    if (temperature.value !== '') body.temperature = Number(temperature.value);
    if (effort.value) body.reasoning_effort = effort.value;
    if (streamBox.checked) {
      body.stream = true;
      body.stream_options = { include_usage: true };
    }

    controller = new AbortController();
    setBusy(true);
    const started = performance.now();
    const info = { firstToken: null };
    let content = '';
    let reasoning = '';
    try {
      const response = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers: { authorization: `Bearer ${key}`, 'content-type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      Object.assign(info, {
        model: response.headers.get('x-gateway-model'),
        upstream: response.headers.get('x-gateway-upstream'),
        attempts: response.headers.get('x-gateway-attempts'),
        requestId: response.headers.get('x-gateway-request-id'),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => null);
        if (response.status === 401) {
          session.playgroundKey = '';
          drawKey();
        }
        throw new ApiError(response.status, data?.error?.message || `HTTP ${response.status}`, data?.error?.code);
      }
      if (body.stream) {
        for await (const event of sseEvents(response)) {
          if (event === '[DONE]') break;
          if (event.error) throw new ApiError(0, event.error.message || '流式响应中断', event.error.code);
          if (event.usage) info.usage = event.usage;
          const choice = event.choices?.[0];
          if (!choice) continue;
          const delta = choice.delta || {};
          if ((delta.content || delta.reasoning_content) && info.firstToken === null) info.firstToken = performance.now() - started;
          if (delta.reasoning_content) {
            reasoning += delta.reasoning_content;
            view.setReasoning(reasoning, true);
          }
          if (delta.content) {
            content += delta.content;
            view.setContent(content);
          }
          if (choice.finish_reason) info.finish = choice.finish_reason;
          scrollDown();
        }
      } else {
        const data = await response.json();
        const choice = data.choices?.[0] || {};
        const message = choice.message || {};
        content = message.content || '';
        if (message.tool_calls) content += `${content ? '\n\n' : ''}工具调用：\n${JSON.stringify(message.tool_calls, null, 2)}`;
        view.setReasoning(message.reasoning_content || '');
        view.setContent(content);
        info.finish = choice.finish_reason;
        info.usage = data.usage;
      }
      info.total = performance.now() - started;
      info.cost = estimateCost(prices[info.model], info.usage);
      history.push({ role: 'assistant', content });
      view.finish(info);
    } catch (error) {
      info.total = performance.now() - started;
      if (error.name === 'AbortError') {
        info.stopped = true;
        if (content) history.push({ role: 'assistant', content });
        else history.pop();
        view.finish(info);
      } else {
        history.pop();
        view.fail(error, info);
      }
    } finally {
      controller = null;
      setBusy(false);
      scrollDown();
    }
  }

  async function loadModels() {
    try {
      const [{ data }, pricing] = await Promise.all([
        admin('/models', { signal: abort.signal }),
        admin('/pricing', { signal: abort.signal }).catch(() => ({ models: {} })),
      ]);
      prices = pricing.models || {};
      const routes = data.filter((m) => m.route);
      const others = data.filter((m) => !m.route);
      clear(modelSelect);
      if (routes.length) {
        modelSelect.append(h('optgroup', { label: '网关路由（别名）' },
          routes.map((m) => h('option', { value: m.id }, `${m.id} → ${m.route.target}`))));
      }
      if (others.length) {
        modelSelect.append(h('optgroup', { label: '上游模型' }, others.map((m) => h('option', { value: m.id }, m.id))));
      }
      if (!data.length) modelSelect.append(h('option', { value: '' }, '没有可用模型（检查上游连接）'));
      if (saved.model && data.some((m) => m.id === saved.model)) modelSelect.value = saved.model;
    } catch (e) {
      if (e.name !== 'AbortError') toast(`加载模型失败：${errorText(e)}`, 'error');
    }
  }

  drawKey();
  reset();
  loadModels();
  return () => {
    abort.abort();
    controller?.abort();
  };
}
