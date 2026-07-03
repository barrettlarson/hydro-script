import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { ApiError, type Health, type Status } from "../api";
import {
  getHealth,
  getStatus,
  postAction,
  postLogin,
  postLogout,
  postTemp,
} from "../api";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getStatus: vi.fn(),
    getHealth: vi.fn(),
    postAction: vi.fn(),
    postTemp: vi.fn(),
    postLogin: vi.fn(),
    postLogout: vi.fn(),
  };
});

const baseStatus: Status = {
  devices: {
    spa_pump: { state: "0", label: "OFF" },
    spa_heater: { state: "0", label: "OFF" },
    spa_set_point: { state: "102", label: "102°F" },
    pool_heater: { state: "0", label: "OFF" },
    pool_set_point: { state: "84", label: "84°F" },
    pool_pump: { state: "1", label: "ON" },
  },
  temps: { air: 75, pool: 82, spa: null },
  setpoint_ranges: { spa: [90, 104], pool: [76, 90] },
  all_keys: [],
};

const okHealth: Health = {
  status: "ok",
  is_stale: false,
  last_success_at: "2026-07-02T12:00:00+00:00",
  last_attempt_at: "2026-07-02T12:00:00+00:00",
  age_seconds: 3,
  consecutive_failures: 0,
  failures_by_category: {},
  recent_failures: [],
};

function card(name: string) {
  const heading = screen.getByRole("heading", { name });
  const section = heading.closest("section");
  if (!section) throw new Error(`no card section for ${name}`);
  return within(section);
}

beforeEach(() => {
  vi.mocked(getStatus).mockResolvedValue(baseStatus);
  vi.mocked(getHealth).mockResolvedValue(okHealth);
  vi.mocked(postAction).mockResolvedValue({
    ok: true,
    action: "x",
    messages: [],
    error: null,
  });
  vi.mocked(postTemp).mockResolvedValue({
    ok: true,
    action: "x",
    messages: [],
    error: null,
  });
  vi.mocked(postLogin).mockResolvedValue({ ok: true });
  vi.mocked(postLogout).mockResolvedValue({ ok: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("App read path", () => {
  it("renders temperatures, dash for unavailable, and the health state", async () => {
    render(<App />);
    expect(await screen.findByText("75")).toBeInTheDocument();
    expect(screen.getByText("82")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();
  });

  it("shows the filter pump running", async () => {
    render(<App />);
    const pump = card("Filter pump");
    await waitFor(() => expect(pump.getByText("Running")).toBeInTheDocument());
    expect(pump.getByRole("button", { name: "Turn off" })).toBeInTheDocument();
  });

  it("shows the warming-up banner on a 503 snapshot", async () => {
    vi.mocked(getStatus).mockRejectedValue(new ApiError(503, "warming up"));
    vi.mocked(getHealth).mockResolvedValue({ ...okHealth, status: "down" });
    render(<App />);
    expect(await screen.findByText(/warming up/i)).toBeInTheDocument();
    expect(screen.getByText("Offline")).toBeInTheDocument();
  });
});

describe("auth gate", () => {
  it("shows the login form instead of the app on a 401", async () => {
    vi.mocked(getStatus).mockRejectedValue(new ApiError(401, "Not authenticated."));
    vi.mocked(getHealth).mockRejectedValue(new ApiError(401, "Not authenticated."));
    render(<App />);
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Spa" })).not.toBeInTheDocument();
  });

  it("signs in and shows the app once the poll succeeds", async () => {
    vi.mocked(getStatus).mockRejectedValue(new ApiError(401, "Not authenticated."));
    vi.mocked(getHealth).mockRejectedValue(new ApiError(401, "Not authenticated."));
    render(<App />);
    const email = await screen.findByLabelText("Email");
    fireEvent.change(email, { target: { value: "user@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "hunter2" },
    });
    // once the session exists, the re-poll succeeds
    vi.mocked(getStatus).mockResolvedValue(baseStatus);
    vi.mocked(getHealth).mockResolvedValue(okHealth);
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() =>
      expect(postLogin).toHaveBeenCalledWith("user@example.com", "hunter2"),
    );
    expect(
      await screen.findByRole("heading", { name: "Spa" }),
    ).toBeInTheDocument();
  });

  it("shows the server's error and stays on the form when login fails", async () => {
    vi.mocked(getStatus).mockRejectedValue(new ApiError(401, "Not authenticated."));
    vi.mocked(getHealth).mockRejectedValue(new ApiError(401, "Not authenticated."));
    vi.mocked(postLogin).mockRejectedValue(
      new ApiError(401, "Invalid email or password."),
    );
    render(<App />);
    const email = await screen.findByLabelText("Email");
    fireEvent.change(email, { target: { value: "user@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "wrong" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(
      await screen.findByText("Invalid email or password."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
  });

  it("signs out from the footer and returns to the login form", async () => {
    render(<App />);
    const signOut = await screen.findByRole("button", { name: "Sign out" });
    // after logout the next poll 401s
    vi.mocked(getStatus).mockRejectedValue(new ApiError(401, "Not authenticated."));
    vi.mocked(getHealth).mockRejectedValue(new ApiError(401, "Not authenticated."));
    fireEvent.click(signOut);
    await waitFor(() => expect(postLogout).toHaveBeenCalled());
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
  });
});

describe("actions", () => {
  it("turns the spa on from its card", async () => {
    render(<App />);
    const spa = card("Spa");
    const button = await spa.findByRole("button", { name: "Turn on" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(postAction).toHaveBeenCalledWith("spa/on"));
  });

  it("turns the pump off from its card", async () => {
    render(<App />);
    const pump = card("Filter pump");
    const button = await pump.findByRole("button", { name: "Turn off" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(postAction).toHaveBeenCalledWith("pump/off"));
  });

  it("shows the API error message when an action fails", async () => {
    vi.mocked(postAction).mockRejectedValue(
      new ApiError(502, "controller unavailable"),
    );
    render(<App />);
    const spa = card("Spa");
    const button = await spa.findByRole("button", { name: "Turn on" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(
      await screen.findByText("controller unavailable"),
    ).toBeInTheDocument();
  });
});

describe("set point slider", () => {
  it("commits once on slider release", async () => {
    render(<App />);
    const slider = await screen.findByRole("slider", {
      name: "spa target temperature",
    });
    await waitFor(() => expect(slider).toBeEnabled());
    fireEvent.change(slider, { target: { value: "96" } });
    fireEvent.change(slider, { target: { value: "98" } });
    expect(postTemp).not.toHaveBeenCalled();
    fireEvent.pointerUp(slider);
    await waitFor(() => expect(postTemp).toHaveBeenCalledTimes(1));
    expect(postTemp).toHaveBeenCalledWith("spa", 98);
  });

  it("coalesces stepper taps into one debounced request", async () => {
    render(<App />);
    const plus = await screen.findByRole("button", {
      name: "Raise pool target",
    });
    await waitFor(() => expect(plus).toBeEnabled());
    vi.useFakeTimers();
    fireEvent.click(plus);
    fireEvent.click(plus);
    fireEvent.click(plus);
    expect(postTemp).not.toHaveBeenCalled();
    vi.advanceTimersByTime(900);
    expect(postTemp).toHaveBeenCalledTimes(1);
    expect(postTemp).toHaveBeenCalledWith("pool", 87);
  });

  it("clamps steppers to the range from the backend", async () => {
    render(<App />);
    const slider = await screen.findByRole("slider", {
      name: "spa target temperature",
    });
    await waitFor(() => expect(slider).toBeEnabled());
    fireEvent.change(slider, { target: { value: "104" } });
    const plus = screen.getByRole("button", { name: "Raise spa target" });
    expect(plus).toBeDisabled();
  });
});
