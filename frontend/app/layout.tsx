import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Trader — Swing Signal Scanner",
  description: "US market swing trade signals powered by technical analysis + AI",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body style={{ minHeight: "100vh", background: "#0d0f14" }}>
        {children}
      </body>
    </html>
  );
}
