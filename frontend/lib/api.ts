export const API_BASE = process.env.NEXT_PUBLIC_API_URL || "";

export interface StrategyBreakdown {
  name: string;
  signal: number;
  score: number;
  reason: string;
  is_trigger: boolean;
}

export interface OptionLeg {
  action: "BUY" | "SELL";
  type: "CALL" | "PUT";
  strike: number;
  price: number;
}

export interface OptionsPlay {
  strategy: string;
  expiry: string;
  dte: number;
  legs: OptionLeg[];
  net_debit: number;
  max_profit: number;
  max_loss: number;
  breakeven: number;
  risk_reward: number;
  prob_profit: number;
  contracts: number;
  cost: number;
  capital_vs_stock: string;
  note: string;
}

export interface Signal {
  ticker: string;
  signal: "BUY" | "SELL" | "WATCH";
  confidence: number;
  entry: number;
  stop_loss: number;
  take_profit_1: number;
  take_profit_2: number;
  atr: number;
  atr_pct: number;
  rsi: number;
  total_score: number;
  macro_score: number;
  holding_days: string;
  support: number;
  resistance: number;
  risk_per_share: number;
  risk_reward: number;
  shares: number;
  position_value: number;
  quality: "high" | "medium" | "low";
  target_note: string;
  triggers: string[];
  reasons: string[];
  strategy_breakdown: StrategyBreakdown[];
  news_summary: string;
  rationale: string;
  options_play: OptionsPlay | null;
}

export interface MacroState {
  score: number;
  summary: string;
  themes: string[];
}

export interface ScanResult {
  signals: Signal[];
  macro: MacroState;
  scanned_at: string | null;
  total_scanned: number;
  scan_running: boolean;
}

export interface StatusResult {
  scanned_at: string | null;
  total_scanned: number;
  signal_count: number;
  scan_running: boolean;
  macro_score: number;
}

async function apiFetch<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`API ${path} → ${res.status}`);
  return res.json();
}

export interface PlanItem {
  ticker: string;
  signal: "BUY" | "SELL";
  kind: "call" | "put";
  strategy: string;
  expiry: string;
  long_strike: number;
  short_strike: number | null;
  structure?: "single" | "spread";
  net_debit: number;
  per_contract: number;
  max_profit: number;
  breakeven: number;
  risk_reward: number;
  prob_profit: number;
  entry_spot: number;
  target: number;
  stop: number;
  quality: string;
  confidence: number;
  contracts: number;
  cost: number;
  max_gain_total: number;
  max_loss_total: number;
  wide_market?: boolean;
  max_spread_pct?: number;
}

export interface Plan {
  budget: number;
  items: PlanItem[];
  total_cost: number;
  cash_left: number;
  n_call_contracts: number;
  n_put_contracts: number;
  note: string;
  priced_at?: string;
}

export interface OpenPosition {
  id: string;
  ticker: string;
  kind: "call" | "put";
  strategy: string;
  expiry: string;
  long_strike: number;
  short_strike: number;
  contracts: number;
  net_debit: number;
  entry_spot: number;
  target: number;
  stop: number;
  opened_at: string;
  spot_now: number;
  days_held: number;
  dte: number;
  cost: number;
  value: number;
  pnl: number;
  pnl_pct: number;
  peak_pnl_pct?: number;
  pct_of_max: number;
  action: "HOLD" | "SELL" | "SCALE";
  urgency: "now" | "watch";
  reason: string;
  sell_contracts?: number;
  structure?: "single" | "spread";
  scaled_out?: string[];
}

export interface Performance {
  closed_total: number;
  tracked_pnl_count: number;
  untracked_count: number;
  open_count: number;
  realized_pnl: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  avg_win: number;
  avg_loss: number;
}

export interface MomentumSignal {
  ticker: string;
  signal: "BUY" | "SELL" | "WATCH";
  score: number;
  confidence: number;
  entry: number;
  stop_loss: number;
  target: number;
  risk_reward: number;
  close_range_pct: number;
  rel_volume: number;
  vwap_dist_pct: number;
  gap_pct: number;
  atr_pct: number;
  hold: string;
  reasons: string[];
}

export interface MomentumResult {
  signals: MomentumSignal[];
  scanned_at: string | null;
  running: boolean;
  note: string;
}

async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`API ${path} → ${res.status}`);
  return res.json();
}

export const api = {
  getSignals: (filter?: "BUY" | "SELL" | "WATCH", limit = 30) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (filter) params.set("signal", filter);
    return apiFetch<ScanResult>(`/api/signals?${params}`);
  },
  getTicker: (ticker: string) => apiFetch<Signal>(`/api/ticker/${ticker}`),
  triggerScan: () =>
    fetch(`${API_BASE}/api/scan`, { method: "POST" }).then((r) => r.json()),
  getStatus: () => apiFetch<StatusResult>("/api/status"),
  buildPlan: (budget: number, structure: "single" | "spread" = "single") =>
    apiPost<Plan>("/api/plan", { budget, structure }),
  savePositions: (items: PlanItem[]) =>
    apiPost<{ status: string; count: number }>("/api/positions", { items }),
  getPositions: () => apiFetch<{ positions: OpenPosition[] }>("/api/positions"),
  closePosition: (id: string, exit_value?: number) =>
    apiPost<{ status: string }>(`/api/positions/${id}/close`, { exit_value: exit_value ?? null }),
  scalePosition: (id: string, contracts: number, exit_value?: number) =>
    apiPost<{ status: string }>(`/api/positions/${id}/scale`, {
      contracts,
      exit_value: exit_value ?? null,
    }),
  getPerformance: () => apiFetch<Performance>("/api/performance"),
  getMomentum: (limit = 20) => apiFetch<MomentumResult>(`/api/momentum?limit=${limit}`),
  scanMomentum: () =>
    fetch(`${API_BASE}/api/momentum/scan`, { method: "POST" }).then((r) => r.json()),
};
