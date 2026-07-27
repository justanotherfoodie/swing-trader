"use client";
import { useState, useEffect, useCallback } from "react";
import { api, MomentumSignal, MomentumResult } from "@/lib/api";

export function MomentumPanel() {
  const [data, setData] = useState<MomentumResult | null>(null);
  const [open, setOpen] = useState(false);
  const [scanning, setScanning] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await api.getMomentum(20));
    } catch {
      /* backend busy */
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [load]);

  async function scan() {
    setScanning(true);
    await api.scanMomentum();
    setTimeout(async () => {
      await load();
      setScanning(false);
    }, 20_000);
  }

  const sigs = data?.signals ?? [];
  const running = scanning || data?.running;

  return (
    <div style={{ background: "#13161e", border: "1px solid #1e2330", borderRadius: 10, padding: 18, marginBottom: 20 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ fontSize: 15, fontWeight: 800, color: "#e2e8f0" }}>⚡ Short-Term Momentum</span>
        <span style={{ fontSize: 11, color: "#6b7280" }}>1–3 day holds · from the last completed session</span>
        <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
          {data?.scanned_at && (
            <span style={{ fontSize: 11, color: "#6b7280" }}>
              {new Date(data.scanned_at).toLocaleTimeString()}
            </span>
          )}
          <button
            onClick={scan}
            disabled={!!running}
            style={{
              background: running ? "#1e2330" : "#2a1f35",
              color: running ? "#6b7280" : "#c084fc",
              border: `1px solid ${running ? "#2a3040" : "rgba(192,132,252,0.35)"}`,
              borderRadius: 6,
              padding: "6px 14px",
              fontSize: 12,
              fontWeight: 600,
              cursor: running ? "not-allowed" : "pointer",
            }}
          >
            {running ? "Scanning…" : "⟳ Scan momentum"}
          </button>
        </span>
      </div>

      {/* The honest constraint — stated up front, not buried */}
      <div style={{ fontSize: 11, color: "#f5a623", background: "rgba(245,166,35,0.08)", border: "1px solid rgba(245,166,35,0.25)", borderRadius: 6, padding: "8px 10px", marginTop: 10, lineHeight: 1.5 }}>
        ⚠ Not for intraday scalping. Free market data is ~15 min delayed, and a US account
        under $25k is capped at <b>3 day trades per rolling 5 business days</b> (PDT rule) —
        a 4th restricts your account. These are next-open entries for 1–3 session holds.
      </div>

      {sigs.length === 0 ? (
        <div style={{ fontSize: 12, color: "#6b7280", marginTop: 12 }}>
          {running ? "Reading the intraday tape…" : "No momentum scan yet — hit “Scan momentum”. Takes ~20s."}
        </div>
      ) : (
        <>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 12 }}>
            {sigs.slice(0, open ? sigs.length : 6).map((m) => (
              <MomentumRow key={m.ticker} m={m} />
            ))}
          </div>
          {sigs.length > 6 && (
            <button
              onClick={() => setOpen(!open)}
              style={{ background: "transparent", color: "#6b7280", border: "none", fontSize: 11, cursor: "pointer", marginTop: 8 }}
            >
              {open ? "Show less" : `Show all ${sigs.length}`}
            </button>
          )}
        </>
      )}
    </div>
  );
}

function MomentumRow({ m }: { m: MomentumSignal }) {
  const buy = m.signal === "BUY";
  const c = buy ? "#00c896" : "#ff4d6d";
  return (
    <div style={{ background: "#0d0f14", border: "1px solid #1e2330", borderLeft: `3px solid ${c}`, borderRadius: 8, padding: "10px 12px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", fontSize: 12 }}>
        <span style={{ fontSize: 10, fontWeight: 800, color: c, background: `${c}1a`, border: `1px solid ${c}44`, borderRadius: 4, padding: "1px 7px" }}>
          {m.signal}
        </span>
        <span style={{ fontWeight: 700, color: "#e2e8f0", fontSize: 14 }}>{m.ticker}</span>
        <span style={{ color: "#6b7280" }}>
          entry <b style={{ color: "#e2e8f0" }}>${m.entry}</b> · stop{" "}
          <b style={{ color: "#ff4d6d" }}>${m.stop_loss}</b> · target{" "}
          <b style={{ color: "#00c896" }}>${m.target}</b>
        </span>
        <span style={{ marginLeft: "auto", display: "flex", gap: 12, color: "#6b7280", fontSize: 11 }}>
          <span title="Where in the day's range it closed — high = buyers in control">
            close <b style={{ color: m.close_range_pct >= 70 ? "#00c896" : m.close_range_pct <= 30 ? "#ff4d6d" : "#94a3b8" }}>{m.close_range_pct}%</b>
          </span>
          <span title="Session volume vs 20-day average">
            vol <b style={{ color: m.rel_volume >= 1.4 ? "#00c896" : "#94a3b8" }}>{m.rel_volume}x</b>
          </span>
          <span title="Close vs session VWAP">
            vwap <b style={{ color: m.vwap_dist_pct > 0 ? "#00c896" : "#ff4d6d" }}>{m.vwap_dist_pct > 0 ? "+" : ""}{m.vwap_dist_pct}%</b>
          </span>
          <span>R:R <b style={{ color: "#e2e8f0" }}>{m.risk_reward}</b></span>
          <span style={{ color: c, fontWeight: 700 }}>{m.confidence}</span>
        </span>
      </div>
      {m.reasons.length > 0 && (
        <div style={{ fontSize: 11, color: "#64748b", marginTop: 5 }}>{m.reasons.slice(0, 2).join(" · ")}</div>
      )}
    </div>
  );
}
