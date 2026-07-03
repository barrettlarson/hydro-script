/// <reference lib="webworker" />
/**
 * Custom service worker (vite-plugin-pwa injectManifest).
 *
 * Replaces the previous generated SW so we can handle Web Push. The precache
 * and navigation behavior deliberately mirror the old generateSW config:
 * app shell precached for instant launch, /api never touched by the SW.
 */
import { clientsClaim } from 'workbox-core'
import {
  cleanupOutdatedCaches,
  createHandlerBoundToURL,
  precacheAndRoute,
  type PrecacheEntry,
} from 'workbox-precaching'
import { NavigationRoute, registerRoute } from 'workbox-routing'

declare let self: ServiceWorkerGlobalScope & {
  __WB_MANIFEST: Array<PrecacheEntry | string>
}

// New SW takes over immediately (matches the old registerType: 'autoUpdate').
self.skipWaiting()
clientsClaim()

precacheAndRoute(self.__WB_MANIFEST)
cleanupOutdatedCaches()

// Navigations fall back to the app shell — except /api, which must stay live.
registerRoute(
  new NavigationRoute(createHandlerBoundToURL('index.html'), {
    denylist: [/^\/api\//],
  }),
)

/** Shape sent by the backend (main.notify_from_snapshot). */
interface PushPayload {
  title?: string
  body?: string
  tag?: string
  ready?: boolean
}

self.addEventListener('push', (event) => {
  let payload: PushPayload = {}
  try {
    payload = (event.data?.json() as PushPayload) ?? {}
  } catch {
    // non-JSON payload; show a generic notification rather than nothing —
    // iOS revokes subscriptions that receive pushes but display nothing
  }
  const ready = payload.ready ?? false
  event.waitUntil(
    self.registration.showNotification(payload.title ?? 'Hydro', {
      body: payload.body,
      icon: '/pwa-192.png',
      badge: '/pwa-192.png',
      // Progress updates share a tag per zone, so each one replaces the last
      // in place (a single notification ticking upward) instead of stacking.
      tag: payload.tag ?? 'hydro',
      // Silent in-place updates while heating; the final "ready" re-alerts.
      renotify: ready,
      silent: !ready,
      data: { url: '/' },
    } as NotificationOptions),
  )
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  event.waitUntil(
    (async () => {
      const clients = await self.clients.matchAll({
        type: 'window',
        includeUncontrolled: true,
      })
      const existing = clients.find((c) => 'focus' in c)
      if (existing) {
        await existing.focus()
      } else {
        await self.clients.openWindow('/')
      }
    })(),
  )
})
