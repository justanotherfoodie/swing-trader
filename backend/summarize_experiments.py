"""Compare the pre-registered walk-forward experiments side by side.

Judged on OUT-OF-SAMPLE expectancy: a change that only helps on the data used to
think of it is noise. A change that helps in BOTH halves is the only kind worth
believing, and even then the sample here is small enough to hold loosely.
"""
import re

RUNS = [
    ("exp_baseline.txt", "BASELINE (all 5 strategies)"),
    ("exp_e1_no_macd.txt", "E1  drop MACD+RSI Confluence"),
    ("exp_e2_medium.txt", "E2  medium-quality signals only"),
    ("exp_e3_widestop.txt", "E3  widen stop to -60%"),
]


def parse(path):
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except FileNotFoundError:
        return None
    lines = [l.strip() for l in text.splitlines()]
    got = {}
    def next_trades_line(idx):
        # The summary line sits a couple of rows below the header, past a separator.
        for j in range(idx + 1, min(idx + 5, len(lines))):
            if lines[j].startswith("Trades "):
                return lines[j]
        return ""

    for i, l in enumerate(lines):
        if l.startswith("IN-SAMPLE ("):
            got["IS"] = next_trades_line(i)
        if l.startswith("OUT-OF-SAMPLE ("):
            got["OOS"] = next_trades_line(i)
    m = re.search(r"EXPECTANCY/trade:\s+\$([-+][\d.,]+)", text)
    p = re.search(r"Profit factor:\s+([\d.]+)", text)
    n = re.search(r"Trades:\s+(\d+)", text)
    got["combined_exp"] = m.group(1) if m else "?"
    got["combined_pf"] = p.group(1) if p else "?"
    got["n"] = n.group(1) if n else "?"
    return got


def num(line, key):
    m = re.search(key + r"\s+\$?([-+]?[\d.,]+)", line or "")
    return float(m.group(1).replace(",", "")) if m else float("nan")


print(f"\n{'='*84}")
print("  PRE-REGISTERED EXPERIMENT COMPARISON  (judge on OUT-OF-SAMPLE)")
print(f"{'='*84}\n")
print(f"  {'run':<34}{'IS exp':>10}{'OOS exp':>10}{'OOS PF':>9}{'combined':>11}{'trades':>8}")
print(f"  {'-'*80}")

for path, name in RUNS:
    g = parse(path)
    if not g:
        print(f"  {name:<34}{'(missing)':>10}")
        continue
    is_e = num(g.get("IS"), "EXPECTANCY")
    oos_e = num(g.get("OOS"), "EXPECTANCY")
    oos_pf = num(g.get("OOS"), r"PF")
    print(f"  {name:<34}{is_e:>+10.2f}{oos_e:>+10.2f}{oos_pf:>9.2f}"
          f"{g['combined_exp']:>11}{g['n']:>8}")

print(f"\n  {'-'*80}")
print("  A result is only believable if BOTH halves improve. One-sided improvements")
print("  are the signature of fitting the past. Samples here are 30-50 trades per")
print("  half - directionally informative, nowhere near statistically conclusive.\n")
