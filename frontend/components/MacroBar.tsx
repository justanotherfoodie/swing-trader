"use client";
import { MacroState } from "@/lib/api";

interface Props {
  macro: MacroState;
  scannedAt: string | null;
  totalScanned: number;
  scanRunning: boolean;
  onScan: () => void;
}

function macroLabel(score: number) {
  if (score >= 1.5) return { label: "Very Bullish", color: "#00c896" };
  if (score >= 0.5) return { label: "Bullish", color: "#4ade80" };
  if (score <= -1.5) return { label: "Very Bearish", color: "#ff4d6d" };
  if (score <= -0.5) return { label: "Bearish", color: "#f87171" };
  return { label: "Neutral", color: "#f5a623" };
}

export function MacroBar({ macro, scannedAt, totalScanned, scanRunning, onScan }: Props) {
  const { label, color } = macroLabel(macro.score);
  const pct = Math.round(((macro.score + 2) / 4) * 100);
  const time = scannedAt ? new Date(scannedAt).toLocaleTimeString() : "—";

  return (
    <div
      style={{
        background: "#13161e",
        border: "1px solid #1e2330",
        borderRadius: 10,
        padding: "12px 18px",
        marginBottom: 20,
        display: "flex",
        gap: 24,
        alignItems: "center",
        flexWrap: "wrap",
      }}
    >
      {/* Macro Score */}
      <div style={{ flex: "0 0 auto" }}>
        <div style={{ fontSize: 11, color: "#6b7280", marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Macro Sentiment
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 18, fontWeight: 700, color }}>{label}</span>
          <span style={{ fontSize: 13, color: "#6b7280" }}>({macro.score >= 0 ? "+" : ""}{macro.score.toFixed(1)})</span>
          {/* Bar */}
          <div style={{ width: 80, height: 6, background: "#1e2330", borderRadius: 3, overflow: "hidden" }}>
            <div style={{ width: `${pct}%`, height: "100%", background: color, borderRadius: 3, transition: "width 0.5s" }} />
          </div>
        </div>
      </div>

      {/* Summary */}
      {macro.summary && (
        <div style={{ flex: 1, minWidth: 200, fontSize: 12, color: "#94a3b8", lineHeight: 1.5 }}>
          {macro.summary}
        </div>
      )}

      {/* Stats */}
      <div style={{ display: "flex", gap: 20, alignItems: "center", marginLeft: "auto" }}>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 11, color: "#6b7280" }}>Stocks scanned</div>
          <div style={{ fontSize: 16, fontWeight: 600 }}>{totalScanned}</div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 11, color: "#6b7280" }}>Last scan</div>
          <div style={{ fontSize: 13, color: "#94a3b8" }}>{time}</div>
        </div>
        <button
          onClick={onScan}
          disabled={scanRunning}
          style={{
            background: scanRunning ? "#1e2330" : "#1e3a2f",
            color: scanRunning ? "#6b7280" : "#00c896",
            border: "1px solid",
            borderColor: scanRunning ? "#2a3040" : "rgba(0,200,150,0.3)",
            borderRadius: 6,
            padding: "6px 14px",
            fontSize: 12,
            fontWeight: 600,
            cursor: scanRunning ? "not-allowed" : "pointer",
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          {scanRunning ? (
            <>
              <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: "#f5a623", animation: "pulse 1s infinite" }} />
              Scanning…
            </>
          ) : (
            "⟳ Rescan"
          )}
        </button>
      </div>

      <style>{`@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }`}</style>
    </div>
  );
}
