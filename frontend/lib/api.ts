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
  short_strike: number;
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
  action: "HOLD" | "SELL";
  urgency: "now" | "watch";
  reason: string;
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
  buildPlan: (budget: number) => apiPost<Plan>("/api/plan", { budget }),
  savePositions: (items: PlanItem[]) =>
    apiPost<{ status: string; count: number }>("/api/positions", { items }),
  getPositions: () => apiFetch<{ positions: OpenPosition[] }>("/api/positions"),
  closePosition: (id: string) =>
    apiPost<{ status: string }>(`/api/positions/${id}/close`, {}),
};
