import { useCallback, useEffect, useState } from 'react'
import { getPushConfig } from './api'
import { disablePush, enablePush, hasBrowserSubscription, pushSupported } from './push'

/**
 * - 'unavailable': browser can't do push, or the server has no VAPID key
 *   configured (or the config fetch failed) — hide the UI entirely.
 * - 'off':         available but not enrolled — show the bell + nudge.
 * - 'enabled':     this device will be notified when its heat-up is ready.
 * - 'denied':      the user blocked notifications in the browser; only they
 *                  can undo that, so show the state but disable the control.
 */
export type PushState = 'unavailable' | 'off' | 'enabled' | 'denied'

export interface PushControls {
  state: PushState
  busy: boolean
  enable: () => Promise<void>
  disable: () => Promise<void>
}

/**
 * Push enrollment state for this device.
 *
 * `active` gates the initial /api/push/config fetch on being logged in —
 * the endpoint is auth-gated like everything else.
 */
export function usePush(active: boolean): PushControls {
  const [state, setState] = useState<PushState>('unavailable')
  const [publicKey, setPublicKey] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!active || !pushSupported()) return
    let cancelled = false
    void (async () => {
      try {
        const config = await getPushConfig()
        if (cancelled || !config.enabled || !config.public_key) return
        setPublicKey(config.public_key)
        if (Notification.permission === 'denied') {
          setState('denied')
          return
        }
        // Enabled only when both halves agree: the browser holds a
        // subscription and the server still has it on file.
        const browserSubscribed = await hasBrowserSubscription()
        if (!cancelled) setState(browserSubscribed && config.subscribed ? 'enabled' : 'off')
      } catch {
        // config fetch failed (e.g. older server) — leave push unavailable
      }
    })()
    return () => {
      cancelled = true
    }
  }, [active])

  const enable = useCallback(async () => {
    if (!publicKey) return
    setBusy(true)
    try {
      setState(await enablePush(publicKey))
    } finally {
      setBusy(false)
    }
  }, [publicKey])

  const disable = useCallback(async () => {
    setBusy(true)
    try {
      await disablePush()
      setState('off')
    } finally {
      setBusy(false)
    }
  }, [])

  return { state, busy, enable, disable }
}
