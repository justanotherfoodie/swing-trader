"use client";
import { useEffect, useState, useCallback } from "react";
import { api, Alert } from "@/lib/api";

/**
 * Anything needing action today, pinned to the top of the page.
 *
 * The exit rules are only worth having if you actually see them fire. Burying a SELL
 * inside an expandable card means the profit ladder does nothing on the days you scroll
 * past it - which are exactly the days it matters.
 */
export function AlertBar() {
  const [alerts, setAlerts] = useState<Alert[]>([]);

  const load = useCallback(async () => {
    try {
      const r = await api.getAlerts();
      setAlerts(r.alerts);
    } catch {
      /* backend may be mid-scan */
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [load]);

  if (alerts.length === 0) {
    return (
      <div style={{ background: "rgba(0,200,150,0.06)", border: "1px solid rgba(0,200,150,0.2)", borderRadius: 10, padding: "10px 16px", marginBottom: 16, fontSize: 12, color: "#00c896" }}>
        ✓ Nothing needs action right now. No positions are hitting a target or a stop.
      </div>
    );
  }

  return (
    <div style={{ background: "rgba(255,77,109,0.08)", border: "1px solid rgba(255,77,109,0.4)", borderRadius: 10, padding: "12px 16px", marginBottom: 16 }}>
      <div style={{ fontSize: 13, fontWeight: 800, color: "#ff4d6d", marginBottom: 8 }}>
        🔔 {alerts.length} thing{alerts.length > 1 ? "s" : ""} need{alerts.length === 1 ? "s" : ""} your attention
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {alerts.map((a, i) => (
          <div key={i} style={{ display: "flex", gap: 10, alignItems: "flex-start", fontSize: 12.5 }}>
            <span style={{
              fontSize: 10, fontWeight: 800, padding: "2px 8px", borderRadius: 4, whiteSpace: "nowrap",
              color: a.level === "risk" ? "#f5a623" : "#ff4d6d",
              background: a.level === "risk" ? "rgba(245,166,35,0.12)" : "rgba(255,77,109,0.14)",
              border: `1px solid ${a.level === "risk" ? "rgba(245,166,35,0.4)" : "rgba(255,77,109,0.4)"}`,
            }}>
              {a.ticker ?? "RISK"}
            </span>
            <span style={{ color: "#e2e8f0", fontWeight: 700, whiteSpace: "nowrap" }}>{a.title}</span>
            <span style={{ color: "#94a3b8", lineHeight: 1.5 }}>{a.detail}</span>
            {a.pnl != null && (
              <span style={{ marginLeft: "auto", color: a.pnl >= 0 ? "#00c896" : "#ff4d6d", fontWeight: 700, whiteSpace: "nowrap" }}>
                {a.pnl >= 0 ? "+" : ""}${a.pnl.toLocaleString()} ({a.pnl_pct}%)
              </span>
            )}
          </div>
        ))}
      </div>
      <div style={{ fontSize: 11, color: "#64748b", marginTop: 8 }}>
        Act on these at the next market open, then mark them in “My Open Positions” below.
      </div>
    </div>
  );
}
