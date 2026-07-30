"use client";
import { useEffect, useState, useCallback } from "react";
import { api, ReviewResult } from "@/lib/api";

/**
 * What you closed recently, next to the conditions you entered under.
 *
 * Most trader improvement comes from reviewing your own fills, not from better signals.
 * Showing the entry context alongside the outcome is what turns "that one lost" into
 * "that one lost AND I entered it at rich IV against the trend" - which is a lesson.
 */
export function WeeklyReview() {
  const [data, setData] = useState<ReviewResult | null>(null);
  const [open, setOpen] = useState(false);
  const [days, setDays] = useState(7);

  const load = useCallback(async (d: number) => {
    try {
      setData(await api.getReview(d));
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    load(days);
  }, [load, days]);

  const closed = data?.closed ?? [];
  const perf = data?.performance;

  return (
    <div style={{ background: "#13161e", border: "1px solid #1e2330", borderRadius: 10, padding: 18, marginBottom: 20 }}>
      <div
        onClick={() => setOpen(!open)}
        style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", flexWrap: "wrap" }}
      >
        <span style={{ fontSize: 15, fontWeight: 800, color: "#e2e8f0" }}>📓 Trade Review</span>
        <span style={{ fontSize: 11, color: "#6b7280" }}>
          What closed, and what you entered into
        </span>
        <span style={{ marginLeft: "auto", display: "flex", gap: 14, alignItems: "center", fontSize: 12 }}>
          {data && (
            <>
              <span style={{ color: "#6b7280" }}>
                {data.closed_count} closed in {data.days}d
              </span>
              {data.tracked_count > 0 && (
                <span style={{ color: data.realized_pnl >= 0 ? "#00c896" : "#ff4d6d", fontWeight: 700 }}>
                  {data.realized_pnl >= 0 ? "+" : ""}${data.realized_pnl.toLocaleString()}
                </span>
              )}
            </>
          )}
          <span style={{ color: "#6b7280", fontSize: 11 }}>{open ? "▲" : "▼"}</span>
        </span>
      </div>

      {open && (
        <div style={{ marginTop: 14 }}>
          <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
            {[7, 30, 90].map((d) => (
              <button
                key={d}
                onClick={() => setDays(d)}
                style={{
                  background: days === d ? "#1a2535" : "transparent",
                  color: days === d ? "#60a5fa" : "#6b7280",
                  border: `1px solid ${days === d ? "rgba(96,165,250,0.3)" : "#1e2330"}`,
                  borderRadius: 6, padding: "4px 12px", fontSize: 11, fontWeight: 600, cursor: "pointer",
                }}
              >
                {d}d
              </button>
            ))}
          </div>

          {closed.length === 0 ? (
            <div style={{ fontSize: 12, color: "#6b7280" }}>
              Nothing closed in the last {days} days.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {closed.map((c, i) => {
                const pnl = c.realized_pnl;
                const col = pnl == null ? "#6b7280" : pnl >= 0 ? "#00c896" : "#ff4d6d";
                return (
                  <div key={i} style={{ background: "#0d0f14", border: "1px solid #1e2330", borderLeft: `3px solid ${col}`, borderRadius: 8, padding: "9px 12px" }}>
                    <div style={{ display: "flex", gap: 10, alignItems: "center", fontSize: 12, flexWrap: "wrap" }}>
                      <b style={{ color: "#e2e8f0" }}>{c.ticker}</b>
                      <span style={{ color: "#6b7280" }}>{c.kind?.toUpperCase()}</span>
                      <span style={{ color: "#6b7280", fontSize: 11 }}>
                        {new Date(c.closed_at).toLocaleDateString()}
                      </span>
                      {c.quality_score != null && (
                        <span style={{ fontSize: 10, color: "#6b7280" }}>Q{c.quality_score}</span>
                      )}
                      {c.iv_verdict && (
                        <span style={{ fontSize: 10, color: c.iv_verdict.includes("rich") ? "#f5a623" : "#6b7280" }}>
                          IV {c.iv_verdict.replace("_", " ")}
                        </span>
                      )}
                      <span style={{ marginLeft: "auto", color: col, fontWeight: 700 }}>
                        {pnl == null ? "no exit price recorded" : `${pnl >= 0 ? "+" : ""}$${pnl.toLocaleString()}`}
                      </span>
                    </div>
                    {(c.strategies.length > 0 || c.warnings_at_entry.length > 0) && (
                      <div style={{ fontSize: 11, color: "#64748b", marginTop: 4 }}>
                        {c.strategies.length > 0 && <>Signals: {c.strategies.join(", ")}. </>}
                        {c.warnings_at_entry.length > 0 && (
                          <span style={{ color: "#f5a623" }}>
                            Warned at entry: {c.warnings_at_entry.join("; ")}
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {data?.prompt && (
            <div style={{ fontSize: 11.5, color: "#94a3b8", marginTop: 12, padding: "9px 12px", background: "#0d0f14", borderRadius: 6, border: "1px solid #1e2330", lineHeight: 1.55 }}>
              <b style={{ color: "#c084fc" }}>Ask yourself:</b> {data.prompt}
            </div>
          )}

          {!!perf?.by_strategy?.length && (
            <div style={{ marginTop: 12 }}>
              <div style={{ fontSize: 10.5, color: "#6b7280", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6 }}>
                All-time by strategy ({perf.tracked_pnl_count} trades with recorded outcomes)
              </div>
              {perf.by_strategy.slice(0, 6).map((a) => (
                <div key={a.key} style={{ display: "flex", gap: 10, fontSize: 11, marginBottom: 2 }}>
                  <span style={{ color: "#94a3b8", minWidth: 190 }}>{a.key}</span>
                  <span style={{ color: a.total_pnl >= 0 ? "#00c896" : "#ff4d6d", fontWeight: 700, minWidth: 70 }}>
                    {a.total_pnl >= 0 ? "+" : ""}${a.total_pnl}
                  </span>
                  <span style={{ color: "#6b7280" }}>{a.trades} trades · {a.win_rate}% win</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
