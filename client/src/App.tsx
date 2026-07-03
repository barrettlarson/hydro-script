import { useEffect, useRef, useState } from "react";
import { postAction, postLogout, postTemp, type Health, type Status } from "./api";
import Login from "./Login";
import { usePolledState } from "./usePolledState";
import { usePush, type PushControls } from "./usePush";

function isOn(status: Status | null, key: string): boolean {
  return status?.devices[key]?.label === "ON";
}

/** Parse a set-point device's state ("102") to a number, or null. */
function setpointOf(status: Status | null, key: string): number | null {
  const raw = status?.devices[key]?.state;
  if (raw == null || raw === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

function TempTile({
  label,
  value,
}: {
  label: string;
  value: number | null | undefined;
}) {
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
          "—"
        )}
      </span>
    </div>
  );
}

function HealthDot({ health }: { health: Health | null }) {
  const level = health?.status ?? "down";
  const title =
    level === "ok"
      ? "Connected"
      : level === "degraded"
        ? "Connection degraded — data may be stale"
        : "Not connected";
  return (
    <span className={`health health-${level}`} title={title}>
      <span className="health-dot" aria-hidden="true" />
      {level === "ok" ? "Live" : level === "degraded" ? "Stale" : "Offline"}
    </span>
  );
}

function BellIcon({ off }: { off: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="20"
      height="20"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
      <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
      {off && <line x1="4" y1="4" x2="20" y2="20" />}
    </svg>
  );
}

/**
 * Header toggle for per-device push notifications. Hidden when push is
 * unavailable (unsupported browser, or server not configured). A denied
 * permission can only be undone in browser settings, so the button just
 * explains itself and stays disabled.
 */
function NotificationBell({
  push,
  onError,
}: {
  push: PushControls;
  onError: (message: string) => void;
}) {
  if (push.state === "unavailable") return null;
  const enabled = push.state === "enabled";
  const title =
    push.state === "denied"
      ? "Notifications are blocked in your browser settings"
      : enabled
        ? "Notifications on for this device — tap to turn off"
        : "Notify this device when the water is ready";
  const toggle = async () => {
    try {
      await (enabled ? push.disable() : push.enable());
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <button
      type="button"
      className={`bell-button${enabled ? " enabled" : ""}`}
      disabled={push.busy || push.state === "denied"}
      title={title}
      aria-label={title}
      aria-pressed={enabled}
      onClick={() => void toggle()}
    >
      <BellIcon off={!enabled} />
    </button>
  );
}

/** Delay before a stepper-tapped set point is sent, so taps coalesce. */
const STEPPER_COMMIT_MS = 900;

interface TempSliderProps {
  zone: "spa" | "pool";
  /** Committed set point from the server, null until known. */
  value: number | null;
  range: [number, number];
  disabled: boolean;
  onCommit: (temp: number) => void;
}

/**
 * Wet-hands-friendly set point control: a fat-thumb slider that commits once
 * on release (no precision taps), plus big +/- steppers whose taps coalesce
 * into a single request. The draft value shows instantly and holds until the
 * server confirms it (or the request fails and the draft is cleared).
 */
function TempSlider({
  zone,
  value,
  range,
  disabled,
  onCommit,
}: TempSliderProps) {
  const [draft, setDraft] = useState<number | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [lo, hi] = range;
  const shown = draft ?? value;

  // Server caught up with the draft — hand display back to server state.
  useEffect(() => {
    if (draft !== null && value === draft) setDraft(null);
  }, [value, draft]);

  useEffect(() => () => clearTimeout(timer.current ?? undefined), []);

  const commit = (temp: number) => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    if (temp !== value) onCommit(temp);
  };

  // Steppers and keyboard arrows adjust one degree at a time, so their
  // commits are debounced to coalesce a burst of taps into one request.
  const scheduleCommit = (temp: number) => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => commit(temp), STEPPER_COMMIT_MS);
  };

  const step = (delta: number) => {
    const base = shown ?? Math.round((lo + hi) / 2);
    const next = Math.min(hi, Math.max(lo, base + delta));
    setDraft(next);
    scheduleCommit(next);
  };

  return (
    <div className="setpoint">
      <button
        type="button"
        className="step-button"
        aria-label={`Lower ${zone} target`}
        disabled={disabled || shown === null || shown <= lo}
        onClick={() => step(-1)}
      >
        −
      </button>
      <input
        type="range"
        className="setpoint-slider"
        min={lo}
        max={hi}
        step={1}
        value={shown ?? lo}
        disabled={disabled || value === null}
        aria-label={`${zone} target temperature`}
        onChange={(e) => setDraft(Number(e.target.value))}
        onPointerUp={() => draft !== null && commit(draft)}
        onKeyUp={() => draft !== null && scheduleCommit(draft)}
      />
      <button
        type="button"
        className="step-button"
        aria-label={`Raise ${zone} target`}
        disabled={disabled || shown === null || shown >= hi}
        onClick={() => step(1)}
      >
        +
      </button>
      <span className="setpoint-value">
        {shown !== null ? `${shown}°` : "—"}
      </span>
    </div>
  );
}

interface ZoneCardProps {
  name: string;
  on: boolean;
  detail: string;
  pending: boolean;
  disabled: boolean;
  onToggle: () => void;
  children?: React.ReactNode;
}

function ZoneCard({
  name,
  on,
  detail,
  pending,
  disabled,
  onToggle,
  children,
}: ZoneCardProps) {
  return (
    <section className={`zone-card${on ? " zone-on" : ""}`}>
      <div className="zone-row">
        <div className="zone-info">
          <h2>{name}</h2>
          <p className="zone-detail">{detail}</p>
        </div>
        <button
          type="button"
          className={`zone-button${on ? " on" : ""}`}
          disabled={disabled}
          onClick={onToggle}
          aria-busy={pending}
        >
          {pending ? (
            <span className="spinner" aria-label="Working…" />
          ) : on ? (
            "Turn off"
          ) : (
            "Turn on"
          )}
        </button>
      </div>
      {children}
    </section>
  );
}

type Pending = "spa" | "pool" | "pump" | "spa-temp" | "pool-temp" | null;

export default function App() {
  const { status, health, warmingUp, unauthenticated, pollError, refresh } =
    usePolledState();
  const [pending, setPending] = useState<Pending>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const push = usePush(!unauthenticated);
  const [nudge, setNudge] = useState<"hidden" | "shown" | "dismissed">(
    "hidden",
  );

  if (unauthenticated) {
    // No HealthDot here: /api/health is gated too, so it would read Offline.
    return (
      <div className="app">
        <header className="app-header">
          <h1>Hydro</h1>
        </header>
        <Login onSuccess={() => void refresh()} />
      </div>
    );
  }

  const spaOn = isOn(status, "spa_pump");
  const spaHeaterOn = isOn(status, "spa_heater");
  const poolHeaterOn = isOn(status, "pool_heater");
  const pumpOn = isOn(status, "pool_pump");
  const spaSetpoint = setpointOf(status, "spa_set_point");
  const poolSetpoint = setpointOf(status, "pool_set_point");

  async function run(
    tag: Exclude<Pending, null>,
    call: () => Promise<unknown>,
  ) {
    setPending(tag);
    setActionError(null);
    try {
      await call();
      await refresh();
      // The backend triggers its own refresh poll after an action; poll once
      // more shortly after so the UI picks up the settled state.
      setTimeout(() => void refresh(), 3000);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setPending(null);
    }
  }

  const toggle = (zone: "spa" | "pool" | "pump", currentlyOn: boolean) => {
    // Starting a heat-up is the moment notifications matter: nudge once if
    // this device could receive them but isn't enrolled yet.
    if (!currentlyOn && zone !== "pump" && push.state === "off") {
      setNudge((n) => (n === "dismissed" ? n : "shown"));
    }
    return run(zone, () => postAction(`${zone}/${currentlyOn ? "off" : "on"}`));
  };
  const setTemp = (zone: "spa" | "pool", temp: number) =>
    run(`${zone}-temp`, () => postTemp(zone, temp));

  const busy = pending !== null || status === null;

  const spaDetail = spaOn
    ? spaHeaterOn
      ? `Heating${spaSetpoint ? ` to ${spaSetpoint}°F` : ""}`
      : "Spa mode on"
    : "Off";
  const poolDetail = poolHeaterOn
    ? `Heating${poolSetpoint ? ` to ${poolSetpoint}°F` : ""}`
    : "Heater off";

  return (
    <div className="app">
      <header className="app-header">
        <h1>Hydro</h1>
        <div className="header-right">
          <NotificationBell push={push} onError={setActionError} />
          <HealthDot health={health} />
        </div>
      </header>

      {actionError && <div className="banner banner-error">{actionError}</div>}
      {!actionError && pollError && (
        <div className="banner banner-error">{pollError}</div>
      )}
      {nudge === "shown" && push.state === "off" && (
        <div className="banner banner-nudge">
          <span>
            Want a ping when the water's ready? Only this device gets
            notified.
          </span>
          <span className="nudge-actions">
            <button
              type="button"
              className="nudge-enable"
              disabled={push.busy}
              onClick={() =>
                void push.enable().catch((e: unknown) => {
                  setActionError(e instanceof Error ? e.message : String(e));
                })
              }
            >
              Notify me
            </button>
            <button
              type="button"
              className="link-button"
              onClick={() => setNudge("dismissed")}
            >
              Not now
            </button>
          </span>
        </div>
      )}
      {warmingUp && !status && (
        <div className="banner">
          Warming up — waiting for the first reading from the pool…
        </div>
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
        pending={pending === "spa"}
        disabled={busy}
        onToggle={() => void toggle("spa", spaOn)}
      >
        <TempSlider
          zone="spa"
          value={spaSetpoint}
          range={status?.setpoint_ranges.spa ?? [90, 104]}
          disabled={busy}
          onCommit={(t) => void setTemp("spa", t)}
        />
      </ZoneCard>
      <ZoneCard
        name="Pool"
        on={poolHeaterOn}
        detail={poolDetail}
        pending={pending === "pool"}
        disabled={busy}
        onToggle={() => void toggle("pool", poolHeaterOn)}
      >
        <TempSlider
          zone="pool"
          value={poolSetpoint}
          range={status?.setpoint_ranges.pool ?? [76, 90]}
          disabled={busy}
          onCommit={(t) => void setTemp("pool", t)}
        />
      </ZoneCard>
      <ZoneCard
        name="Filter pump"
        on={pumpOn}
        detail={pumpOn ? "Running" : "Off"}
        pending={pending === "pump"}
        disabled={busy}
        onToggle={() => void toggle("pump", pumpOn)}
      />

      <footer className="app-footer">
        {health?.last_success_at
          ? `Updated ${new Date(health.last_success_at).toLocaleTimeString()}`
          : "Waiting for first update…"}
        {" · "}
        <button
          type="button"
          className="link-button"
          onClick={() => void postLogout().then(() => refresh())}
        >
          Sign out
        </button>
      </footer>
    </div>
  );
}
