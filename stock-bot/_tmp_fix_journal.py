from pathlib import Path
import csv
from datetime import datetime, timezone

path = Path("data/portal/users/dawimberly/books/alpaca_paper/paper_journal.csv")
bak = path.with_suffix(path.suffix + f".bak_pre_quarantine_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
quarantine = path.with_name("paper_journal_quarantine.csv")

text = path.read_bytes()
bak.write_bytes(text)
print("backup", bak, "bytes", len(text))

# Prefer line-based repair around pandas-reported line 22216
lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
header = lines[0]
# detect expected cols from header via csv
hdr = next(csv.reader([header]))
expected = len(hdr)
print("expected", expected, "total_lines", len(lines))

bad_idxs = []
# Scan with csv.reader on each physical line (simple rows; multiline notes unlikely)
for i, line in enumerate(lines[1:], start=2):
    try:
        row = next(csv.reader([line]))
    except Exception:
        bad_idxs.append(i)
        continue
    if len(row) != expected:
        bad_idxs.append(i)

print("bad_line_count", len(bad_idxs))
print("first_bad", bad_idxs[:20])
for i in bad_idxs[:5]:
    print("BAD", i, repr(lines[i-1][:500]))

# Quarantine and rewrite clean file
q_exists = quarantine.is_file()
with quarantine.open("a" if q_exists else "w", encoding="utf-8", newline="") as qf:
    if not q_exists:
        qf.write(header if header.endswith(("\n","\r\n")) else header + "\n")
        # add meta columns? keep raw line with prefix comment via notes-style sidecar
    for i in bad_idxs:
        raw = lines[i-1]
        if not raw.endswith("\n"):
            raw = raw + "\n"
        qf.write(f"# quarantined_from_line={i}\n")
        qf.write(raw)

keep = [lines[0]] + [ln for i, ln in enumerate(lines[1:], start=2) if i not in set(bad_idxs)]
path.write_text("".join(keep), encoding="utf-8")
print("rewrote", path, "kept_lines", len(keep), "quarantined", len(bad_idxs), "->", quarantine)

# verify pandas
import pandas as pd
df = pd.read_csv(path, low_memory=False)
print("pandas_ok rows", len(df), "cols", len(df.columns))
