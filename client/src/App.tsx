import { useState } from 'react'
import { postAction, type Health, type Status } from './api'
import { usePolledState } from './usePolledState'

function isOn(status: Status | null, key: string): boolean {
  return status?.devices[key]?.label === 'ON'
}

function TempTile({ label, value }: { label: string; value: number | null | undefined }) {
  return (
    <div className="temp-tile">
      <span className="temp-label">{label}</span>
      <span className="temp-value">
        {value != null ? (
          <>
            {value}
            <span className="temp-unit">°F</span>
          </>
        ) : (
          '—'
        )}
      </span>
    </div>
  )
}

function HealthDot({ health }: { health: Health | null }) {
  const level = health?.status ?? 'down'
  const title =
    level === 'ok'
      ? 'Connected'
      : level === 'degraded'
        ? 'Connection degraded — data may be stale'
        : 'Not connected'
  return (
    <span className={`health health-${level}`} title={title}>
      <span className="health-dot" aria-hidden="true" />
      {level === 'ok' ? 'Live' : level === 'degraded' ? 'Stale' : 'Offline'}
    </span>
  )
}

interface ZoneCardProps {
  name: string
  on: boolean
  detail: string
  pending: boolean
  disabled: boolean
  onToggle: () => void
}

function ZoneCard({ name, on, detail, pending, disabled, onToggle }: ZoneCardProps) {
  return (
    <section className={`zone-card${on ? ' zone-on' : ''}`}>
      <div className="zone-info">
        <h2>{name}</h2>
        <p className="zone-detail">{detail}</p>
      </div>
      <button
        type="button"
        className={`zone-button${on ? ' on' : ''}`}
        disabled={disabled}
        onClick={onToggle}
        aria-busy={pending}
      >
        {pending ? <span className="spinner" aria-label="Working…" /> : on ? 'Turn off' : 'Turn on'}
      </button>
    </section>
  )
}

export default function App() {
  const { status, health, warmingUp, pollError, refresh } = usePolledState()
  const [pending, setPending] = useState<'spa' | 'pool' | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const spaOn = isOn(status, 'spa_pump')
  const spaHeaterOn = isOn(status, 'spa_heater')
  const poolHeaterOn = isOn(status, 'pool_heater')
  const spaSetpoint = status?.devices['spa_set_point']?.label
  const poolSetpoint = status?.devices['pool_set_point']?.label

  async function toggle(zone: 'spa' | 'pool', currentlyOn: boolean) {
    setPending(zone)
    setActionError(null)
    try {
      await postAction(`${zone}/${currentlyOn ? 'off' : 'on'}`)
      await refresh()
      // The backend triggers its own refresh poll after an action; poll once
      // more shortly after so the UI picks up the settled state.
      setTimeout(() => void refresh(), 3000)
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e))
    } finally {
      setPending(null)
    }
  }

  const spaDetail = spaOn
    ? spaHeaterOn
      ? `Heating${spaSetpoint ? ` to ${spaSetpoint}` : ''}`
      : 'Spa mode on'
    : 'Off'
  const poolDetail = poolHeaterOn
    ? `Heating${poolSetpoint ? ` to ${poolSetpoint}` : ''}`
    : 'Heater off'

  return (
    <div className="app">
      <header className="app-header">
        <h1>Hydro</h1>
        <HealthDot health={health} />
      </header>

      {actionError && <div className="banner banner-error">{actionError}</div>}
      {!actionError && pollError && <div className="banner banner-error">{pollError}</div>}
      {warmingUp && !status && (
        <div className="banner">Warming up — waiting for the first reading from the pool…</div>
      )}

      <section className="temps" aria-label="Temperatures">
        <TempTile label="Air" value={status?.temps.air} />
        <TempTile label="Pool" value={status?.temps.pool} />
        <TempTile label="Spa" value={status?.temps.spa} />
      </section>

      <ZoneCard
        name="Spa"
        on={spaOn}
        detail={spaDetail}
        pending={pending === 'spa'}
        disabled={pending !== null || status === null}
        onToggle={() => void toggle('spa', spaOn)}
      />
      <ZoneCard
        name="Pool"
        on={poolHeaterOn}
        detail={poolDetail}
        pending={pending === 'pool'}
        disabled={pending !== null || status === null}
        onToggle={() => void toggle('pool', poolHeaterOn)}
      />

      <footer className="app-footer">
        {health?.last_success_at
          ? `Updated ${new Date(health.last_success_at).toLocaleTimeString()}`
          : 'Waiting for first update…'}
      </footer>
    </div>
  )
}
