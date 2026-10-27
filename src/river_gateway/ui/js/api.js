// Client for the gateway's /admin API (admin token) and the playground key.

const TOKEN_KEY = 'rgw.adminToken';
const PLAYGROUND_KEY = 'rgw.playgroundKey';

export const session = {
  get token() {
    return sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY) || '';
  },
  save(token, remember) {
    sessionStorage.setItem(TOKEN_KEY, token);
    if (remember) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  },
  clear() {
    sessionStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(TOKEN_KEY);
  },
  get playgroundKey() {
    return localStorage.getItem(PLAYGROUND_KEY) || '';
  },
  set playgroundKey(value) {
    if (value) localStorage.setItem(PLAYGROUND_KEY, value);
    else localStorage.removeItem(PLAYGROUND_KEY);
  },
};

export class ApiError extends Error {
  constructor(status, message, code = null, body = null) {
    super(message);
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

/** Call /admin{path}. Throws ApiError; a 401 also signals the app to show the login. */
export async function admin(path, { method = 'GET', body, params, signal } = {}) {
  const url = new URL('/admin' + path, location.origin);
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, value);
  }
  const headers = { authorization: `Bearer ${session.token}` };
  if (body !== undefined) headers['content-type'] = 'application/json';
  let response;
  try {
    response = await fetch(url, { method, headers, signal, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError(0, '无法连接网关，请确认网关正在运行。');
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const info = data?.error || {};
    const error = new ApiError(response.status, info.message || `HTTP ${response.status}`, info.code, data);
    if (response.status === 401 && info.code === 'invalid_admin_token') {
      window.dispatchEvent(new CustomEvent('rgw:unauthorized'));
    }
    throw error;
  }
  return data;
}

/** Minutes east of UTC, so the gateway buckets days and hours in the browser's time zone. */
export const tzOffset = () => -new Date().getTimezoneOffset();
