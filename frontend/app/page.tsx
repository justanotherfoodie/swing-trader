"use client";
import { useEffect, useState, useCallback } from "react";
import { api, ScanResult, Signal } from "@/lib/api";
import { MacroBar } from "@/components/MacroBar";
import { SignalCard } from "@/components/SignalCard";
import { TickerSearch } from "@/components/TickerSearch";
import { OptionsPlanner } from "@/components/OptionsPlanner";

type Filter = "ALL" | "BUY" | "SELL" | "WATCH";

const FILTERS: Filter[] = ["ALL", "BUY", "SELL", "WATCH"];

export default function Dashboard() {
  const [data, setData]       = useState<ScanResult | null>(null);
  const [filter, setFilter]   = useState<Filter>("ALL");
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState("");

  const load = useCallback(async (filterVal: Filter = filter) => {
    try {
      const result = await api.getSignals(
        filterVal === "ALL" ? undefined : filterVal,
        40
      );
      setData(result);
      setError("");
    } catch (e) {
      setError("Cannot connect to backend. Make sure the Python server is running on port 8000.");
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    load();
    const interval = setInterval(() => load(), 60_000);
    return () => clearInterval(interval);
  }, [load]);

  const handleFilterChange = (f: Filter) => {
    setFilter(f);
    load(f);
  };

  const handleScan = async () => {
    await api.triggerScan();
    setTimeout(() => load(), 3000);
  };

  const signals: Signal[] = data?.signals ?? [];
  const buys  = signals.filter((s) => s.signal === "BUY").length;
  const sells = signals.filter((s) => s.signal === "SELL").length;

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: "24px 20px" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "baseline", gap: 16, marginBottom: 24 }}>
        <h1 style={{ fontSize: 22, fontWeight: 800, color: "#e2e8f0", margin: 0 }}>
          ⚡ Swing Trader
        </h1>
        <span style={{ fontSize: 13, color: "#6b7280" }}>S&P 500 · Daily signals · 5-strategy engine</span>
      </div>

      {/* Macro bar */}
      {data && (
        <MacroBar
          macro={data.macro}
          scannedAt={data.scanned_at}
          totalScanned={data.total_scanned}
          scanRunning={data.scan_running}
          onScan={handleScan}
        />
      )}

      {/* Guided options workflow: budget -> what to buy -> when to sell */}
      <OptionsPlanner />

      {/* Ticker search */}
      <TickerSearch />

      {/* Summary pills */}
      <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap", alignItems: "center" }}>
        <span style={{ fontSize: 12, color: "#6b7280", marginRight: 4 }}>Filter:</span>
        {FILTERS.map((f) => (
          <button
            key={f}
            onClick={() => handleFilterChange(f)}
            style={{
              padding: "5px 14px",
              borderRadius: 20,
              fontSize: 12,
              fontWeight: 600,
              cursor: "pointer",
              border: "1px solid",
              borderColor: filter === f
                ? (f === "BUY" ? "#00c896" : f === "SELL" ? "#ff4d6d" : f === "WATCH" ? "#f5a623" : "#60a5fa")
                : "#1e2330",
              background: filter === f ? "rgba(255,255,255,0.05)" : "transparent",
              color: filter === f
                ? (f === "BUY" ? "#00c896" : f === "SELL" ? "#ff4d6d" : f === "WATCH" ? "#f5a623" : "#60a5fa")
                : "#6b7280",
            }}
          >
            {f}{f === "BUY" && data ? ` (${buys})` : f === "SELL" && data ? ` (${sells})` : ""}
          </button>
        ))}
        <span style={{ marginLeft: "auto", fontSize: 12, color: "#6b7280" }}>
          {signals.length} signals shown
        </span>
      </div>

      {/* States */}
      {loading && (
        <div style={{ textAlign: "center", padding: "60px 0", color: "#6b7280" }}>
          <div style={{ fontSize: 32, marginBottom: 12 }}>⟳</div>
          Running initial scan across S&P 500… (takes ~60s)
        </div>
      )}

      {error && (
        <div style={{
          background: "rgba(255,77,109,0.08)",
          border: "1px solid rgba(255,77,109,0.3)",
          borderRadius: 10,
          padding: "16px 20px",
          color: "#ff4d6d",
          fontSize: 14,
        }}>
          {error}
        </div>
      )}

      {/* Signal list */}
      {!loading && !error && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {signals.length === 0 ? (
            <div style={{ textAlign: "center", padding: "40px 0", color: "#6b7280" }}>
              No {filter !== "ALL" ? filter : ""} signals found in current scan.
            </div>
          ) : (
            signals.map((s) => <SignalCard key={s.ticker} signal={s} />)
          )}
        </div>
      )}

      {/* Footer */}
      <div style={{ marginTop: 40, textAlign: "center", fontSize: 11, color: "#374151", lineHeight: 1.8 }}>
        For personal use only · Not financial advice · Always manage your own risk
      </div>
    </div>
  );
}
