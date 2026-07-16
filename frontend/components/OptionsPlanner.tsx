"use client";
import { useState, useEffect, useCallback } from "react";
import { api, Plan, PlanItem, OpenPosition } from "@/lib/api";

export function OptionsPlanner() {
  const [budget, setBudget] = useState(600);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [loadingPlan, setLoadingPlan] = useState(false);
  const [positions, setPositions] = useState<OpenPosition[]>([]);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  const loadPositions = useCallback(async () => {
    try {
      const r = await api.getPositions();
      setPositions(r.positions);
    } catch {
      /* backend may be mid-scan */
    }
  }, []);

  useEffect(() => {
    loadPositions();
    const t = setInterval(loadPositions, 60_000);
    return () => clearInterval(t);
  }, [loadPositions]);

  async function build() {
    setLoadingPlan(true);
    setError("");
    setSaved(false);
    try {
      const p = await api.buildPlan(budget);
      setPlan(p);
    } catch {
      setError("Couldn't build a plan — make sure a scan has finished.");
    } finally {
      setLoadingPlan(false);
    }
  }

  async function execute() {
    if (!plan || plan.items.length === 0) return;
    await api.savePositions(plan.items);
    setSaved(true);
    setPlan(null);
    setTimeout(loadPositions, 500);
  }

  async function close(id: string) {
    await api.closePosition(id);
    setTimeout(loadPositions, 400);
  }

  const sells = positions.filter((p) => p.action === "SELL");

  return (
    <div style={{ background: "#13161e", border: "1px solid #1e2330", borderRadius: 10, padding: 18, marginBottom: 20 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
        <span style={{ fontSize: 15, fontWeight: 800, color: "#e2e8f0" }}>💼 Options Plan & Positions</span>
        <span style={{ fontSize: 11, color: "#6b7280" }}>Tell me your budget — I'll tell you what to buy and when to sell</span>
      </div>

      {/* Budget input */}
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 12, flexWrap: "wrap" }}>
        <span style={{ fontSize: 13, color: "#94a3b8" }}>My budget:</span>
        <div style={{ display: "flex", alignItems: "center", background: "#0d0f14", border: "1px solid #1e2330", borderRadius: 8, padding: "6px 10px" }}>
          <span style={{ color: "#6b7280", marginRight: 4 }}>$</span>
          <input
            type="number"
            value={budget}
            min={100}
            step={100}
            onChange={(e) => setBudget(Number(e.target.value))}
            style={{ width: 90, background: "transparent", border: "none", color: "#e2e8f0", fontSize: 15, fontWeight: 600, outline: "none" }}
          />
        </div>
        <button
          onClick={build}
          disabled={loadingPlan}
          style={{ background: "#1a2535", color: "#60a5fa", border: "1px solid rgba(96,165,250,0.3)", borderRadius: 8, padding: "8px 16px", fontSize: 13, fontWeight: 600, cursor: "pointer" }}
        >
          {loadingPlan ? "Building…" : "Build my plan →"}
        </button>
        {error && <span style={{ color: "#ff4d6d", fontSize: 12 }}>{error}</span>}
        {saved && <span style={{ color: "#00c896", fontSize: 12 }}>✓ Saved to your positions below</span>}
      </div>

      {/* The plan (shopping list) */}
      {plan && (
        <div style={{ marginTop: 14, padding: 14, background: "#0d0f14", borderRadius: 8, border: "1px solid #2a3550" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, marginBottom: 12 }}>
            <div style={{ fontSize: 12, color: "#94a3b8" }}>{plan.note}</div>
            {plan.priced_at && (
              <div style={{ fontSize: 10.5, color: "#6b7280", whiteSpace: "nowrap" }}>
                Live prices as of {new Date(plan.priced_at).toLocaleTimeString()} — buy soon after building, prices move at the open.
              </div>
            )}
          </div>
          {plan.items.length === 0 ? (
            <div style={{ color: "#6b7280", fontSize: 13 }}>No affordable spreads for this budget right now.</div>
          ) : (
            <>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {plan.items.map((it, i) => (
                  <PlanRow key={i} it={it} />
                ))}
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 12, paddingTop: 12, borderTop: "1px solid #1e2330" }}>
                <span style={{ fontSize: 12, color: "#94a3b8" }}>
                  Total cost <b style={{ color: "#e2e8f0" }}>${plan.total_cost.toLocaleString()}</b> · cash left ${plan.cash_left.toLocaleString()}
                </span>
                <button
                  onClick={execute}
                  style={{ background: "#1e3a2f", color: "#00c896", border: "1px solid rgba(0,200,150,0.4)", borderRadius: 8, padding: "8px 16px", fontSize: 13, fontWeight: 700, cursor: "pointer" }}
                >
                  ✓ I bought these — track them
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {/* Open positions */}
      {positions.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
            <span style={{ fontSize: 12, color: "#6b7280", textTransform: "uppercase", letterSpacing: "0.06em" }}>My Open Positions</span>
            {sells.length > 0 && (
              <span style={{ fontSize: 11, fontWeight: 700, color: "#ff4d6d", background: "rgba(255,77,109,0.12)", border: "1px solid rgba(255,77,109,0.3)", borderRadius: 12, padding: "1px 8px" }}>
                {sells.length} to SELL now
              </span>
            )}
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {positions.map((p) => (
              <PositionRow key={p.id} p={p} onClose={() => close(p.id)} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function PlanRow({ it }: { it: PlanItem }) {
  const c = it.kind === "call" ? "#00c896" : "#ff4d6d";
  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13 }}>
        <span style={{ fontSize: 11, fontWeight: 700, color: c, background: `${c}1a`, border: `1px solid ${c}44`, borderRadius: 4, padding: "1px 7px", minWidth: 78, textAlign: "center" }}>
          {it.contracts}× {it.kind === "call" ? "CALL" : "PUT"}
        </span>
        <span style={{ fontWeight: 700, color: "#e2e8f0", minWidth: 50 }}>{it.ticker}</span>
        <span style={{ color: "#94a3b8" }}>
          ${it.long_strike}/${it.short_strike} spread · exp {it.expiry}
        </span>
        {it.wide_market && (
          <span style={{ fontSize: 10, fontWeight: 700, color: "#f5a623", background: "rgba(245,166,35,0.12)", border: "1px solid rgba(245,166,35,0.4)", borderRadius: 4, padding: "1px 6px" }}>
            ⚠ WIDE MARKET
          </span>
        )}
        <span style={{ marginLeft: "auto", color: "#6b7280", fontSize: 12 }}>
          cost <b style={{ color: "#e2e8f0" }}>${it.cost.toLocaleString()}</b> · max gain <span style={{ color: "#00c896" }}>${it.max_gain_total.toLocaleString()}</span> · POP {it.prob_profit}%
        </span>
      </div>
      {it.wide_market && (
        <div style={{ fontSize: 11, color: "#f5a623", marginTop: 3, marginLeft: 88 }}>
          Bid/ask spread is {((it.max_spread_pct || 0) * 100).toFixed(0)}% wide — use a limit order, your fill will likely differ from this quote.
        </div>
      )}
    </div>
  );
}

function PositionRow({ p, onClose }: { p: OpenPosition; onClose: () => void }) {
  const sell = p.action === "SELL";
  const accent = sell ? "#ff4d6d" : "#00c896";
  const pnlColor = p.pnl >= 0 ? "#00c896" : "#ff4d6d";
  return (
    <div style={{ background: "#0d0f14", border: `1px solid ${sell ? "#ff4d6d55" : "#1e2330"}`, borderLeft: `3px solid ${accent}`, borderRadius: 8, padding: "12px 14px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ fontSize: 11, fontWeight: 800, color: accent, background: `${accent}1a`, border: `1px solid ${accent}55`, borderRadius: 4, padding: "2px 9px" }}>
          {sell ? "▼ SELL NOW" : "● HOLD"}
        </span>
        <span style={{ fontWeight: 700, color: "#e2e8f0" }}>{p.ticker}</span>
        <span style={{ fontSize: 12, color: "#94a3b8" }}>
          {p.contracts}× {p.kind.toUpperCase()} ${p.long_strike}/${p.short_strike} · exp {p.expiry}
        </span>
        <span style={{ marginLeft: "auto", display: "flex", gap: 14, alignItems: "center", fontSize: 12 }}>
          <span style={{ color: "#6b7280" }}>now <b style={{ color: "#e2e8f0" }}>${p.spot_now}</b></span>
          <span style={{ color: "#6b7280" }}>value <b style={{ color: "#e2e8f0" }}>${p.value.toLocaleString()}</b></span>
          <span style={{ color: pnlColor, fontWeight: 700 }}>
            {p.pnl >= 0 ? "+" : ""}${p.pnl.toLocaleString()} ({p.pnl_pct}%)
          </span>
          {!!p.peak_pnl_pct && p.peak_pnl_pct > p.pnl_pct + 3 && (
            <span style={{ color: "#6b7280", fontSize: 11 }}>peak +{p.peak_pnl_pct}%</span>
          )}
          <button
            onClick={onClose}
            title="Mark as closed / sold"
            style={{ background: "transparent", color: "#6b7280", border: "1px solid #1e2330", borderRadius: 6, padding: "3px 9px", fontSize: 11, cursor: "pointer" }}
          >
            Close
          </button>
        </span>
      </div>
      <div style={{ fontSize: 12, color: sell ? "#ffb3c0" : "#94a3b8", marginTop: 8, lineHeight: 1.5 }}>
        {sell ? "🔔 " : ""}{p.reason}
      </div>
    </div>
  );
}
