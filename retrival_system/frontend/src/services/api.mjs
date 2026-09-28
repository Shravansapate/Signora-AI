export class ApiError extends Error {
  constructor(message, { status = 0, requestId = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.requestId = requestId;
  }
}

/** Credentials are supplied per request and are never persisted or placed in URLs. */
export async function apiRequest(path, { token, body, method, signal, timeoutMs = 30000 } = {}) {
  if (!path.startsWith('/api/v1/') || path.includes('\\') || /[\r\n]/.test(path)) {
    throw new ApiError('Invalid API path.');
  }
  if (typeof token !== 'string' || !token.trim()) throw new ApiError('Enter an access token.');
  const timeout = AbortSignal.timeout(timeoutMs);
  const requestSignal = signal ? AbortSignal.any([signal, timeout]) : timeout;
  let response;
  const multipart = typeof FormData !== 'undefined' && body instanceof FormData;
  try {
    response = await fetch(path, {
      method: method ?? (body === undefined ? 'GET' : 'POST'),
      headers: { Authorization: `Bearer ${token}`, Accept: 'application/json',
        ...(body === undefined || multipart ? {} : { 'Content-Type': 'application/json' }) },
      body: body === undefined ? undefined : multipart ? body : JSON.stringify(body),
      credentials: 'omit', cache: 'no-store', redirect: 'error', signal: requestSignal,
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    if (timeout.aborted) throw new ApiError('The server took too long. Try again.');
    throw new ApiError('Cannot reach the backend. Check that the service is running.');
  }
  const requestId = response.headers.get('x-request-id');
  let data;
  try { data = await response.json(); }
  catch { throw new ApiError('The server returned an unreadable response.', { status: response.status, requestId }); }
  if (!response.ok) {
    const fallback = response.status === 401 ? 'Access token was not accepted.' :
      response.status === 403 ? 'This token does not have permission for this action.' :
      response.status === 409 ? 'This plan is no longer available. Prepare a fresh plan.' : 'The request could not be completed.';
    const detail = typeof data?.detail === 'string' ? data.detail : Array.isArray(data?.detail)
      ? data.detail.map(item => `${(item.loc ?? []).filter(part => part !== 'body').join('.')}: ${item.msg}`).join('; ') : null;
    throw new ApiError(detail || fallback, { status: response.status, requestId });
  }
  return data;
}

export async function revalidatePlan(plan, { token, signal }) {
  const current = await apiRequest(`/api/v1/playback/${encodeURIComponent(plan.manifest_id)}`, { token, signal });
  if (current.manifest_id !== plan.manifest_id || current.manifest_hash !== plan.manifest_hash) {
    throw new ApiError('The playback plan changed. Prepare a fresh plan.');
  }
  return current;
}
