import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, getHealth, getStatus, postAction, postTemp } from '../api'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const fetchMock = vi.fn()
vi.stubGlobal('fetch', fetchMock)

afterEach(() => {
  fetchMock.mockReset()
})

describe('getStatus', () => {
  it('returns the parsed status payload', async () => {
    const payload = { devices: {}, temps: { air: 75 }, setpoint_ranges: {}, all_keys: [] }
    fetchMock.mockResolvedValue(jsonResponse(payload))
    await expect(getStatus()).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith('/api/status', undefined)
  })

  it('throws ApiError with the server detail on failure', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'warming up' }, 503))
    const err = await getStatus().catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(503)
    expect(err.message).toBe('warming up')
  })

  it('falls back to a generic message for non-JSON error bodies', async () => {
    fetchMock.mockResolvedValue(new Response('gateway exploded', { status: 502 }))
    const err = await getStatus().catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err.message).toBe('Request failed (HTTP 502)')
  })
})

describe('getHealth', () => {
  it('fetches /api/health', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ status: 'ok' }))
    await getHealth()
    expect(fetchMock).toHaveBeenCalledWith('/api/health', undefined)
  })
})

describe('postAction', () => {
  it('POSTs to the action path', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true, action: 'spa-on', messages: [] }))
    await postAction('spa/on')
    expect(fetchMock).toHaveBeenCalledWith('/api/spa/on', { method: 'POST' })
  })
})

describe('postTemp', () => {
  it('POSTs the target as a JSON body', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true, action: 'spa-temp', messages: [] }))
    await postTemp('spa', 100)
    expect(fetchMock).toHaveBeenCalledWith('/api/spa/temp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ temp: 100 }),
    })
  })

  it('surfaces the 422 detail for out-of-range targets', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'Set point must be 90-104°F.' }, 422))
    const err = await postTemp('spa', 80).catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err.message).toBe('Set point must be 90-104°F.')
  })
})
