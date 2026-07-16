"use client";
import { useState } from "react";
import { Signal, OptionsPlay } from "@/lib/api";

interface Props {
  signal: Signal;
}

const STRATEGY_COLORS: Record<number, string> = {
  1: "#00c896",
  [-1]: "#ff4d6d",
  0: "#6b7280",
};

export function SignalCard({ signal: s }: Props) {
  const [expanded, setExpanded] = useState(false);

  const isBuy  = s.signal === "BUY";
  const isSell = s.signal === "SELL";
  const accentColor = isBuy ? "#00c896" : isSell ? "#ff4d6d" : "#f5a623";
  const badgeClass  = isBuy ? "badge-buy" : isSell ? "badge-sell" : "badge-watch";

  const rsiColor = s.rsi < 30 ? "#00c896" : s.rsi > 70 ? "#ff4d6d" : "#94a3b8";

  return (
    <div
      style={{
        background: "#13161e",
        border: `1px solid ${expanded ? accentColor + "55" : "#1e2330"}`,
        borderLeft: `3px solid ${accentColor}`,
        borderRadius: 10,
        overflow: "hidden",
        transition: "border-color 0.2s",
      }}
    >
      {/* Header row */}
      <div
        onClick={() => setExpanded(!expanded)}
        style={{
          display: "grid",
          gridTemplateColumns: "92px 1fr auto auto auto auto auto",
          alignItems: "center",
          gap: 12,
          padding: "14px 16px",
          cursor: "pointer",
        }}
      >
        {/* Ticker + Signal + Quality badge */}
        <div>
          <div style={{ fontSize: 17, fontWeight: 700, letterSpacing: "0.02em" }}>{s.ticker}</div>
          <div style={{ display: "flex", gap: 4, marginTop: 3, alignItems: "center" }}>
            <span
              className={badgeClass}
              style={{ fontSize: 10, fontWeight: 700, padding: "2px 7px", borderRadius: 20, display: "inline-block" }}
            >
              {s.signal}
            </span>
            <QualityDot quality={s.quality} />
          </div>
        </div>

        {/* Rationale preview + target warning */}
        <div>
          <div style={{ fontSize: 12, color: "#94a3b8", lineHeight: 1.4, overflow: "hidden", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }}>
            {s.rationale || s.reasons[0] || "—"}
          </div>
          {s.target_note && (
            <div style={{ fontSize: 10.5, color: s.target_note.includes("Poor") ? "#ff4d6d" : "#f5a623", marginTop: 3 }}>
              ⚠ {s.target_note}
            </div>
          )}
        </div>

        {/* Entry */}
        <div style={{ textAlign: "right", minWidth: 64 }}>
          <div style={{ fontSize: 10, color: "#6b7280", marginBottom: 2 }}>ENTRY</div>
          <div style={{ fontSize: 15, fontWeight: 600 }}>${s.entry.toFixed(2)}</div>
        </div>

        {/* Stop Loss */}
        <div style={{ textAlign: "right", minWidth: 64 }}>
          <div style={{ fontSize: 10, color: "#6b7280", marginBottom: 2 }}>STOP</div>
          <div style={{ fontSize: 15, fontWeight: 600, color: "#ff4d6d" }}>${s.stop_loss.toFixed(2)}</div>
        </div>

        {/* Target */}
        <div style={{ textAlign: "right", minWidth: 64 }}>
          <div style={{ fontSize: 10, color: "#6b7280", marginBottom: 2 }}>TARGET</div>
          <div style={{ fontSize: 15, fontWeight: 600, color: "#00c896" }}>${s.take_profit_1.toFixed(2)}</div>
        </div>

        {/* R:R + shares */}
        <div style={{ textAlign: "right", minWidth: 58 }}>
          <div style={{ fontSize: 10, color: "#6b7280", marginBottom: 2 }}>R:R</div>
          <div style={{ fontSize: 15, fontWeight: 600, color: s.risk_reward >= 2 ? "#00c896" : s.risk_reward >= 1.5 ? "#f5a623" : "#ff4d6d" }}>
            {s.risk_reward.toFixed(1)}
          </div>
          <div style={{ fontSize: 10, color: "#6b7280", marginTop: 2 }}>{s.shares} sh</div>
        </div>

        {/* Confidence */}
        <div style={{ textAlign: "center", minWidth: 56 }}>
          <ConfidenceRing value={s.confidence} color={accentColor} />
        </div>
      </div>

      {/* Expanded detail */}
      {expanded && (
        <div style={{ padding: "0 16px 16px", borderTop: "1px solid #1e2330" }}>
          <div style={{ paddingTop: 14, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
            {/* Left: price levels */}
            <div>
              <div style={{ fontSize: 11, color: "#6b7280", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>
                Trade Levels
              </div>
              <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
                <tbody>
                  {[
                    ["Entry", `$${s.entry.toFixed(2)}`, "#e2e8f0"],
                    ["Stop Loss", `$${s.stop_loss.toFixed(2)}`, "#ff4d6d"],
                    ["Target 1", `$${s.take_profit_1.toFixed(2)}`, "#00c896"],
                    ["Target 2 (3R)", `$${s.take_profit_2.toFixed(2)}`, "#4ade80"],
                    ["Risk / share", `$${s.risk_per_share.toFixed(2)}`, "#94a3b8"],
                    ["R:R to TP1", `${s.risk_reward.toFixed(2)} : 1`, s.risk_reward >= 2 ? "#00c896" : s.risk_reward >= 1.5 ? "#f5a623" : "#ff4d6d"],
                    ["Shares ($200 risk)", `${s.shares}`, "#e2e8f0"],
                    ["Position value", `$${s.position_value.toLocaleString()}`, "#94a3b8"],
                    ["ATR (volatility)", `$${s.atr.toFixed(2)} (${s.atr_pct}%)`, "#94a3b8"],
                    ["RSI(14)", s.rsi.toFixed(1), rsiColor],
                    ["Hold Est.", s.holding_days, "#94a3b8"],
                    ["Support", `$${s.support.toFixed(2)}`, "#94a3b8"],
                    ["Resistance", `$${s.resistance.toFixed(2)}`, "#94a3b8"],
                  ].map(([label, val, color]) => (
                    <tr key={label}>
                      <td style={{ color: "#6b7280", paddingBottom: 6, paddingRight: 12 }}>{label}</td>
                      <td style={{ color, fontWeight: 600, paddingBottom: 6 }}>{val}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Right: strategy breakdown */}
            <div>
              <div style={{ fontSize: 11, color: "#6b7280", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>
                Strategy Signals
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {s.strategy_breakdown.map((st) => (
                  <div key={st.name}>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 3, alignItems: "center" }}>
                      <span style={{ fontSize: 12, color: "#94a3b8", display: "flex", alignItems: "center", gap: 5 }}>
                        {st.name}
                        {st.is_trigger && (
                          <span style={{ fontSize: 9, fontWeight: 700, color: "#f5a623", border: "1px solid rgba(245,166,35,0.4)", borderRadius: 4, padding: "0 4px" }}>
                            TRIGGER
                          </span>
                        )}
                      </span>
                      <span style={{ fontSize: 12, fontWeight: 600, color: STRATEGY_COLORS[st.signal] ?? "#6b7280" }}>
                        {st.signal === 1 ? "▲ BUY" : st.signal === -1 ? "▼ SELL" : "— NEUTRAL"}
                      </span>
                    </div>
                    <div style={{ height: 4, background: "#1e2330", borderRadius: 2, overflow: "hidden" }}>
                      <div style={{
                        width: `${Math.abs(st.score) / 2 * 100}%`,
                        height: "100%",
                        background: STRATEGY_COLORS[st.signal] ?? "#6b7280",
                        borderRadius: 2,
                      }} />
                    </div>
                    <div style={{ fontSize: 11, color: "#6b7280", marginTop: 2 }}>{st.reason}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Options Play */}
          {s.options_play && <OptionsPlayBlock play={s.options_play} accent={accentColor} />}

          {/* AI Rationale */}
          {s.rationale && (
            <div style={{ marginTop: 14, padding: "10px 14px", background: "#0d0f14", borderRadius: 8, border: "1px solid #1e2330" }}>
              <div style={{ fontSize: 10, color: "#6b7280", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6 }}>
                AI Rationale
              </div>
              <div style={{ fontSize: 13, color: "#94a3b8", lineHeight: 1.6 }}>{s.rationale}</div>
            </div>
          )}

          {/* News summary */}
          {s.news_summary && (
            <div style={{ marginTop: 10, fontSize: 12, color: "#64748b", lineHeight: 1.5, fontStyle: "italic" }}>
              News: {s.news_summary}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function OptionsPlayBlock({ play, accent }: { play: OptionsPlay; accent: string }) {
  return (
    <div style={{ marginTop: 14, padding: "12px 14px", background: "#0d0f14", borderRadius: 8, border: `1px solid ${accent}44` }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
        <div style={{ fontSize: 11, color: accent, textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 700 }}>
          ⚙ Options Play — {play.strategy}
        </div>
        <div style={{ fontSize: 11, color: "#6b7280" }}>
          Exp {play.expiry} · {play.dte}d
        </div>
      </div>

      {/* Legs */}
      <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 10 }}>
        {play.legs.map((l, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}>
            <span style={{
              fontSize: 10, fontWeight: 700, padding: "1px 6px", borderRadius: 4,
              color: l.action === "BUY" ? "#00c896" : "#ff4d6d",
              background: l.action === "BUY" ? "rgba(0,200,150,0.12)" : "rgba(255,77,109,0.12)",
            }}>
              {l.action}
            </span>
            <span style={{ color: "#e2e8f0", fontWeight: 600 }}>
              ${l.strike.toFixed(0)} {l.type}
            </span>
            <span style={{ color: "#6b7280" }}>@ ${l.price.toFixed(2)}</span>
          </div>
        ))}
      </div>

      {/* Stats grid */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 8, fontSize: 12 }}>
        {[
          ["Net cost", `$${(play.net_debit * 100).toFixed(0)}/ct`, "#e2e8f0"],
          ["Max gain", `$${(play.max_profit * 100).toFixed(0)}/ct`, "#00c896"],
          ["Max loss", `$${(play.max_loss * 100).toFixed(0)}/ct`, "#ff4d6d"],
          ["R:R", `${play.risk_reward.toFixed(1)} : 1`, play.risk_reward >= 2 ? "#00c896" : "#f5a623"],
          ["Breakeven", `$${play.breakeven.toFixed(2)}`, "#94a3b8"],
          ["Prob. profit", `${play.prob_profit}%`, play.prob_profit >= 50 ? "#00c896" : "#f5a623"],
          ["Contracts", `${play.contracts}`, "#e2e8f0"],
          ["Total cost", `$${play.cost.toLocaleString()}`, "#94a3b8"],
        ].map(([label, val, color]) => (
          <div key={label}>
            <div style={{ fontSize: 9.5, color: "#6b7280", marginBottom: 2 }}>{label}</div>
            <div style={{ color, fontWeight: 600 }}>{val}</div>
          </div>
        ))}
      </div>

      <div style={{ fontSize: 11, color: "#64748b", marginTop: 10, lineHeight: 1.5 }}>
        {play.note} <span style={{ color: accent }}>{play.capital_vs_stock}.</span>
      </div>
    </div>
  );
}

function QualityDot({ quality }: { quality: "high" | "medium" | "low" }) {
  const map = {
    high:   { c: "#00c896", label: "A" },
    medium: { c: "#f5a623", label: "B" },
    low:    { c: "#6b7280", label: "C" },
  };
  const q = map[quality];
  return (
    <span
      title={`${quality} quality setup`}
      style={{
        fontSize: 9, fontWeight: 800, color: q.c,
        border: `1px solid ${q.c}66`, borderRadius: 4, padding: "1px 5px",
        background: `${q.c}1a`,
      }}
    >
      {q.label}
    </span>
  );
}

function ConfidenceRing({ value, color }: { value: number; color: string }) {
  const r = 20;
  const circ = 2 * Math.PI * r;
  const offset = circ - (value / 100) * circ;
  return (
    <div style={{ position: "relative", width: 48, height: 48, display: "flex", alignItems: "center", justifyContent: "center" }}>
      <svg width={48} height={48} style={{ position: "absolute", top: 0, left: 0 }}>
        <circle cx={24} cy={24} r={r} fill="none" stroke="#1e2330" strokeWidth={4} />
        <circle
          cx={24} cy={24} r={r} fill="none"
          stroke={color} strokeWidth={4}
          strokeDasharray={circ}
          strokeDashoffset={offset}
          strokeLinecap="round"
          transform="rotate(-90 24 24)"
        />
      </svg>
      <span style={{ fontSize: 12, fontWeight: 700, color, zIndex: 1 }}>{value}</span>
    </div>
  );
}
