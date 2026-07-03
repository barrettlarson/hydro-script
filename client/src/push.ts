/**
 * Web Push enrollment helpers.
 *
 * The subscription is per-browser/per-device, which is exactly the targeting
 * the backend wants: "notify the device that pressed the button". On iOS this
 * only works from the home-screen-installed PWA (16.4+); in a plain Safari
 * tab PushManager is absent and `pushSupported()` reports false.
 */
import { postPushSubscribe, postPushUnsubscribe } from './api'

export function pushSupported(): boolean {
  return (
    'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window
  )
}

/** Decode a base64url VAPID public key into the bytes subscribe() expects. */
export function urlBase64ToUint8Array(base64url: string): Uint8Array {
  const padding = '='.repeat((4 - (base64url.length % 4)) % 4)
  const base64 = (base64url + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(base64)
  return Uint8Array.from(raw, (c) => c.charCodeAt(0))
}

/**
 * Ask for permission, subscribe with the server's VAPID key, and register the
 * subscription with the backend. Returns 'denied' if the user refuses the
 * permission prompt (or has blocked notifications previously).
 */
export async function enablePush(publicKey: string): Promise<'enabled' | 'denied'> {
  const permission = await Notification.requestPermission()
  if (permission !== 'granted') return 'denied'
  const registration = await navigator.serviceWorker.getRegistration()
  if (!registration) {
    // No SW registration (e.g. dev server) — nothing to attach a push to.
    throw new Error('Notifications need the installed app.')
  }
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(publicKey).buffer as ArrayBuffer,
  })
  await postPushSubscribe(subscription.toJSON())
  return 'enabled'
}

/** Drop the browser-side subscription and tell the backend to forget it. */
export async function disablePush(): Promise<void> {
  const registration = await navigator.serviceWorker.getRegistration()
  const subscription = await registration?.pushManager.getSubscription()
  await subscription?.unsubscribe()
  await postPushUnsubscribe()
}

/** Whether this browser currently holds a push subscription. */
export async function hasBrowserSubscription(): Promise<boolean> {
  const registration = await navigator.serviceWorker.getRegistration()
  const subscription = await registration?.pushManager.getSubscription()
  return subscription != null
}
