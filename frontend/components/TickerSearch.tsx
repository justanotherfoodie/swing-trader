"use client";
import { useState } from "react";
import { Signal, api } from "@/lib/api";
import { SignalCard } from "./SignalCard";

export function TickerSearch() {
  const [query, setQuery]   = useState("");
  const [result, setResult] = useState<Signal | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError]   = useState("");

  async function search() {
    const t = query.trim().toUpperCase();
    if (!t) return;
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const data = await api.getTicker(t);
      setResult(data);
    } catch {
      setError(`No data found for ${t}`);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={{ marginBottom: 24 }}>
      <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && search()}
          placeholder="Search any ticker, e.g. AAPL"
          style={{
            flex: 1,
            background: "#13161e",
            border: "1px solid #1e2330",
            borderRadius: 8,
            padding: "10px 14px",
            color: "#e2e8f0",
            fontSize: 14,
            outline: "none",
          }}
        />
        <button
          onClick={search}
          disabled={loading}
          style={{
            background: "#1a2535",
            color: "#60a5fa",
            border: "1px solid rgba(96,165,250,0.3)",
            borderRadius: 8,
            padding: "10px 18px",
            fontSize: 14,
            fontWeight: 600,
            cursor: loading ? "not-allowed" : "pointer",
          }}
        >
          {loading ? "…" : "Analyze"}
        </button>
      </div>
      {error && <div style={{ color: "#ff4d6d", fontSize: 13 }}>{error}</div>}
      {result && <SignalCard signal={result} />}
    </div>
  );
}
