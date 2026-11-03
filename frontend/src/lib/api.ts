export type Job = {
  id: string; type: string; status: string; progress: number; progress_message: string;
  result?: any; error?: string | null; created_at?: string | null
}

function csrfToken() {
  const match = document.cookie.match(/(?:^|; )kronos_csrf=([^;]+)/)
  return match ? decodeURIComponent(match[1]) : ''
}

export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers || {})
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (init.method && !['GET', 'HEAD', 'OPTIONS'].includes(init.method.toUpperCase())) {
    headers.set('X-CSRF-Token', csrfToken())
  }
  const response = await fetch(path, { ...init, headers, credentials: 'same-origin' })
  if (response.status === 401) throw new Error('AUTH_REQUIRED')
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`)
  return body as T
}
