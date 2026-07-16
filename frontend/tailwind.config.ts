import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: "#0d0f14",
        surface: "#13161e",
        border: "#1e2330",
        buy: "#00c896",
        sell: "#ff4d6d",
        watch: "#f5a623",
        muted: "#6b7280",
      },
      fontFamily: {
        mono: ["var(--font-mono)", "monospace"],
      },
    },
  },
  plugins: [],
};
export default config;
