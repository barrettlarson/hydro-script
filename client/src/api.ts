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

/**
 * The server sends more (failure history, attempt timestamps, streak counts).
 * Declaring only what we read keeps this an honest statement of coupling: a
 * field absent here is one the server can change without touching the client.
 * See FastAPI's /docs for the full response.
 */
export interface Health {
  status: 'ok' | 'degraded' | 'down'
  /** When the snapshot was last refreshed — not merely when a call last
   *  succeeded. An action succeeds without producing new data. */
  last_snapshot_at: string | null
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