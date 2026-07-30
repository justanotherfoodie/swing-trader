"use client";
import { useState, useEffect, useCallback } from "react";
import { api, Plan, PlanItem, OpenPosition, Performance } from "@/lib/api";

export function OptionsPlanner() {
  const [budget, setBudget] = useState(600);
  const [structure, setStructure] = useState<"single" | "spread" | "credit">("single");
  const [source, setSource] = useState<"swing" | "momentum" | "both">("both");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [loadingPlan, setLoadingPlan] = useState(false);
  const [positions, setPositions] = useState<OpenPosition[]>([]);
  const [perf, setPerf] = useState<Performance | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  const loadPositions = useCallback(async () => {
    try {
      const r = await api.getPositions();
      setPositions(r.positions);
    } catch {
      /* backend may be mid-scan */
    }
    try {
      setPerf(await api.getPerformance());
    } catch {
      /* ignore */
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
      const p = await api.buildPlan(budget, structure, source);
      setPlan(p);
    } catch {
      setError("Couldn't build a plan — make sure a scan has finished.");
    } finally {
      setLoadingPlan(false);
    }
  }

  async function scale(id: string, contracts: number, ticker: string, perContract: number) {
    const est = perContract * contracts;
    const raw = window.prompt(
      `Selling ${contracts} contract(s) of ${ticker}.\n\n` +
      `Total $ you received for those ${contracts}?\n` +
      `(Current estimate: $${est.toFixed(2)} — edit to your real fill.)`,
      est.toFixed(2),
    );
    if (raw === null) return;
    const val = raw.trim() === "" ? undefined : Number(raw);
    if (val !== undefined && (!isFinite(val) || val < 0)) {
      alert("That doesn't look like a dollar amount. Nothing was changed.");
      return;
    }
    await api.scalePosition(id, contracts, val);
    setTimeout(loadPositions, 400);
  }

  async function execute() {
    if (!plan || plan.items.length === 0) return;
    await api.savePositions(plan.items);
    setSaved(true);
    setPlan(null);
    setTimeout(loadPositions, 500);
  }

  // Closing without capturing what you actually sold for leaves realized P&L unknown,
  // which silently starves the whole feedback loop: the review, the win rate, and the
  // per-strategy attribution all stay empty forever. So ask, every time.
  async function close(id: string, ticker: string, estValue: number) {
    const raw = window.prompt(
      `Closing ${ticker}.\n\nTotal $ you received for the whole position?\n` +
      `(Current estimate: $${estValue.toFixed(2)} — edit to your real fill.)\n\n` +
      `Leave blank to skip, but then this trade won't count toward your stats.`,
      estValue.toFixed(2),
    );
    if (raw === null) return;                       // cancelled - don't close
    const val = raw.trim() === "" ? undefined : Number(raw);
    if (val !== undefined && (!isFinite(val) || val < 0)) {
      alert("That doesn't look like a dollar amount. Nothing was changed.");
      return;
    }
    await api.closePosition(id, val);
    setTimeout(loadPositions, 400);
  }

  const actionable = positions.filter((p) => p.action === "SELL" || p.action === "SCALE");

  return (
    <div style={{ background: "#13161e", border: "1px solid #1e2330", borderRadius: 10, padding: 18, marginBottom: 20 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4, flexWrap: "wrap" }}>
        <span style={{ fontSize: 15, fontWeight: 800, color: "#e2e8f0" }}>💼 Options Plan & Positions</span>
        <span style={{ fontSize: 11, color: "#6b7280" }}>Tell me your budget — I&apos;ll tell you what to buy and when to sell</span>
        {perf && perf.tracked_pnl_count > 0 && (
          <span style={{ marginLeft: "auto", fontSize: 11, color: "#6b7280" }}>
            Realized:{" "}
            <b style={{ color: perf.realized_pnl >= 0 ? "#00c896" : "#ff4d6d" }}>
              {perf.realized_pnl >= 0 ? "+" : ""}${perf.realized_pnl.toLocaleString()}
            </b>{" "}
            · {perf.wins}W/{perf.losses}L
            {perf.win_rate !== null && ` (${perf.win_rate}%)`}
          </span>
        )}
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

        {/* Structure toggle — buying premium vs selling it */}
        <div style={{ display: "flex", background: "#0d0f14", border: "1px solid #1e2330", borderRadius: 8, overflow: "hidden" }}>
          {(["single", "spread", "credit"] as const).map((s) => {
            const label = s === "single" ? "Buy call/put" : s === "spread" ? "Debit spread" : "Sell credit";
            const tip = {
              single: "One long call/put — needs a real move; whole premium at risk",
              spread: "Vertical debit spread — cheaper directional bet, capped both ways",
              credit: "Credit spread — collect premium, win if the stock stays put. Wins often, loses bigger. Always defined-risk.",
            }[s];
            const isRec = plan?.advice?.recommended === s;
            return (
              <button
                key={s}
                onClick={() => setStructure(s)}
                title={tip}
                style={{
                  background: structure === s ? "#1a2535" : "transparent",
                  color: structure === s ? "#60a5fa" : "#6b7280",
                  border: "none",
                  padding: "7px 11px",
                  fontSize: 12,
                  fontWeight: 600,
                  cursor: "pointer",
                  position: "relative",
                }}
              >
                {label}
                {isRec && <span title="Recommended for today's market" style={{ color: "#00c896", marginLeft: 4 }}>★</span>}
              </button>
            );
          })}
        </div>

        {/* Which scanner feeds the plan */}
        <div style={{ display: "flex", background: "#0d0f14", border: "1px solid #1e2330", borderRadius: 8, overflow: "hidden" }}>
          {(["both", "swing", "momentum"] as const).map((s) => (
            <button
              key={s}
              onClick={() => setSource(s)}
              title={{
                both: "Use both scanners — daily swing signals and short-term momentum",
                swing: "Daily swing signals only (5-strategy engine, 5-10 day holds)",
                momentum: "Short-term momentum only (intraday tape, 1-3 day holds)",
              }[s]}
              style={{
                background: source === s ? "#251f35" : "transparent",
                color: source === s ? "#c084fc" : "#6b7280",
                border: "none", padding: "7px 11px", fontSize: 12, fontWeight: 600, cursor: "pointer",
              }}
            >
              {s === "both" ? "Both" : s === "swing" ? "Swing" : "Momentum"}
            </button>
          ))}
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
          {/* Account-level risk. A halted or throttled account overrides every
              individual signal below it. */}
          {plan.risk && (plan.risk.status !== "normal" || plan.risk.undersized) && (
            <div style={{
              fontSize: 12,
              color: plan.risk.status === "halted" ? "#ffb3c0" : "#ffd89b",
              background: plan.risk.status === "halted" ? "rgba(255,77,109,0.1)" : "rgba(245,166,35,0.08)",
              border: `1px solid ${plan.risk.status === "halted" ? "rgba(255,77,109,0.35)" : "rgba(245,166,35,0.3)"}`,
              borderRadius: 6, padding: "10px 12px", marginBottom: 10, lineHeight: 1.55,
            }}>
              <b style={{ color: plan.risk.status === "halted" ? "#ff4d6d" : "#f5a623" }}>
                {plan.risk.status === "halted" ? "🛑 Trading halted" :
                 plan.risk.status === "throttled" ? "⚠ Size throttled" : "⚠ Account undersized"}
              </b>
              {" "}· equity ${plan.risk.equity.toLocaleString()} · drawdown {plan.risk.drawdown_pct}%
              {" "}· heat {plan.risk.heat_pct}%/{plan.risk.max_heat_pct}%
              {plan.risk.messages.map((m, i) => (
                <div key={i} style={{ marginTop: 4, color: "#94a3b8" }}>{m}</div>
              ))}
            </div>
          )}

          {/* Which structure suits today — the answer to "app says don't trade". */}
          {plan.advice && plan.advice.recommended !== plan.structure && (
            <div style={{ fontSize: 12, color: "#c084fc", background: "rgba(192,132,252,0.08)", border: "1px solid rgba(192,132,252,0.3)", borderRadius: 6, padding: "9px 12px", marginBottom: 10, lineHeight: 1.5 }}>
              <b>Consider &ldquo;{plan.advice.recommended === "credit" ? "Sell credit" : plan.advice.recommended === "single" ? "Buy call/put" : "Debit spread"}&rdquo; instead.</b>{" "}
              {plan.advice.why}
            </div>
          )}

          {/* Environment check — the single most useful thing the app can say is
              "today is a bad day to buy premium at all". */}
          {plan.environment && !plan.environment.favourable && structure !== "credit" && (
            <div style={{ fontSize: 12, color: "#ffb3c0", background: "rgba(255,77,109,0.1)", border: "1px solid rgba(255,77,109,0.35)", borderRadius: 6, padding: "10px 12px", marginBottom: 10, lineHeight: 1.55 }}>
              <b style={{ color: "#ff4d6d" }}>⛔ Poor conditions for buying options.</b>{" "}
              {plan.environment.note.replace("POOR conditions for buying options - consider sitting out or trading much smaller. ", "")}
              <div style={{ marginTop: 4, color: "#94a3b8" }}>
                Long options need a trending index or expanding volatility. Sideways chop
                bleeds them via time decay regardless of how good the signal looks. Consider
                sitting out or trading much smaller today.
              </div>
            </div>
          )}
          {plan.environment?.favourable && (
            <div style={{ fontSize: 11, color: "#00c896", marginBottom: 8 }}>
              ✓ {plan.environment.note}
            </div>
          )}
          {plan.regime && (
            <div style={{ fontSize: 11, color: "#94a3b8", marginBottom: 10, paddingBottom: 8, borderBottom: "1px solid #1e2330" }}>
              <b style={{ color: plan.regime.bias > 0 ? "#00c896" : plan.regime.bias < 0 ? "#ff4d6d" : "#f5a623" }}>
                Market: {plan.regime.regime.replace("_", " ")}
              </b>{" "}
              — {plan.regime.note}
            </div>
          )}
          {plan.items.length === 0 ? (
            <div style={{ color: "#6b7280", fontSize: 13 }}>
              No trades cleared the quality filters at this budget. That is a result, not a
              failure — see what was rejected below.
            </div>
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

          {/* Why the plan is quiet — rejected setups, so a short list is explained
              rather than looking like the scanner found nothing. */}
          {!!plan.rejected?.length && (
            <div style={{ marginTop: 12, paddingTop: 10, borderTop: "1px solid #1e2330" }}>
              <div style={{ fontSize: 10.5, color: "#6b7280", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6 }}>
                Filtered out ({plan.rejected.length}) — good direction, bad contract
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                {plan.rejected.map((r) => (
                  <div key={r.ticker} style={{ fontSize: 11, color: "#64748b" }}>
                    <b style={{ color: "#94a3b8" }}>{r.ticker}</b>{" "}
                    <span style={{ color: "#ff4d6d" }}>Q{r.quality_score}</span> — {r.warnings.join("; ")}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Open positions */}
      {positions.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
            <span style={{ fontSize: 12, color: "#6b7280", textTransform: "uppercase", letterSpacing: "0.06em" }}>My Open Positions</span>
            {actionable.length > 0 && (
              <span style={{ fontSize: 11, fontWeight: 700, color: "#ff4d6d", background: "rgba(255,77,109,0.12)", border: "1px solid rgba(255,77,109,0.3)", borderRadius: 12, padding: "1px 8px" }}>
                {actionable.length} needs action
              </span>
            )}
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {positions.map((p) => (
              <PositionRow
                key={p.id}
                p={p}
                onClose={() => close(p.id, p.ticker, p.value)}
                onScale={(n) =>
                  scale(p.id, n, p.ticker, p.contracts ? p.value / p.contracts : 0)
                }
              />
            ))}
          </div>

          {/* Feedback loop: which strategies actually made money. Builds up as you
              close trades — the only honest measure of the engine's edge. */}
          {!!perf?.by_strategy?.length && (
            <div style={{ marginTop: 14, paddingTop: 10, borderTop: "1px solid #1e2330" }}>
              <div style={{ fontSize: 10.5, color: "#6b7280", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6 }}>
                What&apos;s actually working ({perf.tracked_pnl_count} closed trades)
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                {perf.by_strategy.slice(0, 6).map((a) => (
                  <div key={a.key} style={{ display: "flex", gap: 10, fontSize: 11, alignItems: "center" }}>
                    <span style={{ color: "#94a3b8", minWidth: 190 }}>{a.key}</span>
                    <span style={{ color: a.total_pnl >= 0 ? "#00c896" : "#ff4d6d", fontWeight: 700, minWidth: 70 }}>
                      {a.total_pnl >= 0 ? "+" : ""}${a.total_pnl}
                    </span>
                    <span style={{ color: "#6b7280" }}>
                      {a.trades} trade{a.trades !== 1 ? "s" : ""} · {a.win_rate}% win · avg{" "}
                      {a.avg_pnl >= 0 ? "+" : ""}${a.avg_pnl}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function PlanRow({ it }: { it: PlanItem }) {
  const c = it.kind === "call" ? "#00c896" : "#ff4d6d";
  const q = it.quality_score ?? 100;
  const qc = q >= 75 ? "#00c896" : q >= 50 ? "#f5a623" : "#ff4d6d";
  const warns = it.warnings ?? [];
  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13, flexWrap: "wrap" }}>
        <span style={{ fontSize: 11, fontWeight: 700, color: c, background: `${c}1a`, border: `1px solid ${c}44`, borderRadius: 4, padding: "1px 7px", minWidth: 78, textAlign: "center" }}>
          {it.contracts}× {it.kind === "call" ? "CALL" : "PUT"}
        </span>
        <span style={{ fontWeight: 700, color: "#e2e8f0", minWidth: 50 }}>{it.ticker}</span>
        <span style={{ color: "#94a3b8" }}>
          {it.short_strike == null
            ? `$${it.long_strike} ${it.kind.toUpperCase()}`
            : `$${it.long_strike}/$${it.short_strike} spread`}{" "}
          · exp {it.expiry}
        </span>
        <span
          title="Trade quality after IV, earnings, liquidity and trend checks"
          style={{ fontSize: 10, fontWeight: 800, color: qc, background: `${qc}1a`, border: `1px solid ${qc}55`, borderRadius: 4, padding: "1px 6px" }}
        >
          Q{q}
        </span>
        {it.delta != null && it.delta > 0 && (
          <span style={{ fontSize: 10, color: "#6b7280" }} title="Option delta — how much it moves per $1 of stock">
            Δ{it.delta.toFixed(2)}
          </span>
        )}
        {it.iv_verdict && (
          <span
            title={`Implied vol vs the stock's actual movement${it.iv_ratio ? ` (${it.iv_ratio}x)` : ""}`}
            style={{
              fontSize: 10,
              color: it.iv_verdict.includes("rich") ? "#f5a623" : it.iv_verdict === "cheap" ? "#00c896" : "#6b7280",
            }}
          >
            IV {it.iv_verdict.replace("_", " ")}
          </span>
        )}
        <span style={{ marginLeft: "auto", color: "#6b7280", fontSize: 12 }}>
          cost <b style={{ color: "#e2e8f0" }}>${it.cost.toLocaleString()}</b> · max gain <span style={{ color: "#00c896" }}>${it.max_gain_total.toLocaleString()}</span> · POP {it.prob_profit}%
        </span>
      </div>
      {warns.length > 0 && (
        <div style={{ fontSize: 11, color: "#f5a623", marginTop: 3, marginLeft: 88, lineHeight: 1.5 }}>
          ⚠ {warns.join(" · ")}
        </div>
      )}
      {it.limit_guidance && (
        <div style={{ fontSize: 11, color: "#60a5fa", marginTop: 3, marginLeft: 88, lineHeight: 1.5 }}>
          🎯 {it.limit_guidance.instruction}
        </div>
      )}
    </div>
  );
}

function PositionRow({
  p,
  onClose,
  onScale,
}: {
  p: OpenPosition;
  onClose: () => void;
  onScale: (contracts: number) => void;
}) {
  const sell = p.action === "SELL";
  const scale = p.action === "SCALE";
  const alert = sell || scale;
  const accent = sell ? "#ff4d6d" : scale ? "#f5a623" : "#00c896";
  const pnlColor = p.pnl >= 0 ? "#00c896" : "#ff4d6d";
  const legs =
    p.structure === "single" || p.short_strike == null
      ? `$${p.long_strike} ${p.kind.toUpperCase()}`
      : `$${p.long_strike}/$${p.short_strike} ${p.kind.toUpperCase()}`;

  return (
    <div style={{ background: "#0d0f14", border: `1px solid ${alert ? accent + "55" : "#1e2330"}`, borderLeft: `3px solid ${accent}`, borderRadius: 8, padding: "12px 14px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ fontSize: 11, fontWeight: 800, color: accent, background: `${accent}1a`, border: `1px solid ${accent}55`, borderRadius: 4, padding: "2px 9px" }}>
          {sell ? "▼ SELL ALL" : scale ? `◑ SELL ${p.sell_contracts} of ${p.contracts}` : "● HOLD"}
        </span>
        <span style={{ fontWeight: 700, color: "#e2e8f0" }}>{p.ticker}</span>
        <span style={{ fontSize: 12, color: "#94a3b8" }}>
          {p.contracts}× {legs} · exp {p.expiry}
        </span>
        {!!p.scaled_out?.length && (
          <span style={{ fontSize: 10, color: "#00c896", border: "1px solid rgba(0,200,150,0.3)", borderRadius: 4, padding: "1px 5px" }}>
            banked {p.scaled_out.join("+")}
          </span>
        )}
        <span style={{ marginLeft: "auto", display: "flex", gap: 14, alignItems: "center", fontSize: 12 }}>
          <span style={{ color: "#6b7280" }}>now <b style={{ color: "#e2e8f0" }}>${p.spot_now}</b></span>
          <span style={{ color: "#6b7280" }}>value <b style={{ color: "#e2e8f0" }}>${p.value.toLocaleString()}</b></span>
          <span style={{ color: pnlColor, fontWeight: 700 }}>
            {p.pnl >= 0 ? "+" : ""}${p.pnl.toLocaleString()} ({p.pnl_pct}%)
          </span>
          {!!p.peak_pnl_pct && p.peak_pnl_pct > p.pnl_pct + 3 && (
            <span style={{ color: "#6b7280", fontSize: 11 }}>peak +{p.peak_pnl_pct}%</span>
          )}
          {scale && (
            <button
              onClick={() => onScale(p.sell_contracts || 1)}
              title="Record the partial sale — keeps the runner open"
              style={{ background: "rgba(245,166,35,0.12)", color: "#f5a623", border: "1px solid rgba(245,166,35,0.4)", borderRadius: 6, padding: "3px 9px", fontSize: 11, fontWeight: 700, cursor: "pointer" }}
            >
              Sold {p.sell_contracts}
            </button>
          )}
          <button
            onClick={onClose}
            title="Mark the whole position as closed / sold"
            style={{ background: "transparent", color: "#6b7280", border: "1px solid #1e2330", borderRadius: 6, padding: "3px 9px", fontSize: 11, cursor: "pointer" }}
          >
            Close
          </button>
        </span>
      </div>
      <div style={{ fontSize: 12, color: sell ? "#ffb3c0" : scale ? "#ffd89b" : "#94a3b8", marginTop: 8, lineHeight: 1.5 }}>
        {alert ? "🔔 " : ""}{p.reason}
      </div>
    </div>
  );
}
