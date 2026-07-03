import { useCallback, useEffect, useState } from 'react'
import { ApiError, getHealth, getStatus, type Health, type Status } from './api'

/** How often to re-poll /api/status + /api/health while the app is visible. */
const POLL_MS = 15_000

export interface PolledState {
  status: Status | null
  health: Health | null
  /** True while the backend has no snapshot yet (503 from /api/status). */
  warmingUp: boolean
  /** Message for an unexpected polling failure (not warm-up). */
  pollError: string | null
  /** Force an immediate refresh (used after actions). */
  refresh: () => Promise<void>
}

/**
 * Poll the backend read endpoints on an interval.
 *
 * The two fetches are settled independently so a 503 from /api/status
 * (cache warming up) doesn't discard a perfectly good health payload.
 * Polling pauses while the tab is hidden and refreshes on return.
 */
export function usePolledState(): PolledState {
  const [status, setStatus] = useState<Status | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  const [warmingUp, setWarmingUp] = useState(false)
  const [pollError, setPollError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    const [s, h] = await Promise.allSettled([getStatus(), getHealth()])
    if (h.status === 'fulfilled') setHealth(h.value)
    if (s.status === 'fulfilled') {
      setStatus(s.value)
      setWarmingUp(false)
      setPollError(null)
    } else if (s.reason instanceof ApiError && s.reason.status === 503) {
      setWarmingUp(true)
      setPollError(null)
    } else {
      setPollError(s.reason instanceof Error ? s.reason.message : String(s.reason))
    }
  }, [])

  useEffect(() => {
    void refresh()
    const id = setInterval(() => {
      if (!document.hidden) void refresh()
    }, POLL_MS)
    const onVisibility = () => {
      if (!document.hidden) void refresh()
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      clearInterval(id)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [refresh])

  return { status, health, warmingUp, pollError, refresh }
}
