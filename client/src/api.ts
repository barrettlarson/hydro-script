/**
 * Typed client for the hydro-script API.
 *
 * All paths are same-origin: the Vite dev server proxies /api to FastAPI,
 * and in production FastAPI serves the built client itself.
 */

export interface DeviceInfo {
  state: string | null
  label: string
}

export interface Temps {
  air: number | null
  pool: number | null
  spa: number | null
}

export interface Status {
  devices: Record<string, DeviceInfo>
  temps: Temps
  setpoint_ranges: { spa: [number, number]; pool: [number, number] }
  all_keys: string[]
}

export interface FailureRecord {
  ts: string | null
  category: string
  detail: string
}

export interface Health {
  status: 'ok' | 'degraded' | 'down'
  is_stale: boolean
  last_success_at: string | null
  last_attempt_at: string | null
  age_seconds: number | null
  consecutive_failures: number
  failures_by_category: Record<string, number>
  recent_failures: FailureRecord[]
}

export interface ActionResult {
  ok: boolean
  action: string
  messages: string[]
  error: string | null
}

export type ActionPath = 'spa/on' | 'spa/off' | 'pool/on' | 'pool/off' | 'pump/on' | 'pump/off'

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init)
  if (!res.ok) {
    let detail = `Request failed (HTTP ${res.status})`
    try {
      const body = await res.json()
      if (typeof body?.detail === 'string') detail = body.detail
    } catch {
      // non-JSON error body; keep the generic message
    }
    throw new ApiError(res.status, detail)
  }
  return res.json() as Promise<T>
}

export interface AuthResult {
  ok: boolean
}

export const getStatus = () => request<Status>('/api/status')
export const getHealth = () => request<Health>('/api/health')
export const postAction = (path: ActionPath) =>
  request<ActionResult>(`/api/${path}`, { method: 'POST' })
export const postTemp = (zone: 'spa' | 'pool', temp: number) =>
  request<ActionResult>(`/api/${zone}/temp`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ temp }),
  })
export interface PushConfig {
  enabled: boolean
  public_key: string | null
  subscribed: boolean
}

export const getPushConfig = () => request<PushConfig>('/api/push/config')
// `subscription` is the browser's PushSubscription.toJSON(), passed through opaque.
export const postPushSubscribe = (subscription: unknown) =>
  request<AuthResult>('/api/push/subscribe', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ subscription }),
  })
export const postPushUnsubscribe = () =>
  request<AuthResult>('/api/push/unsubscribe', { method: 'POST' })
// The session rides on a same-origin cookie, which fetch sends by default.
export const postLogin = (email: string, password: string) =>
  request<AuthResult>('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
export const postLogout = () => request<AuthResult>('/api/logout', { method: 'POST' })