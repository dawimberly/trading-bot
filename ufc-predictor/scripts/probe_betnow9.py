import re
import requests

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
r = requests.get("https://www.betnow.eu/sportsbook-info/fighting/ufc/", headers=headers, timeout=30)
text = r.text
# real american odds: +digits or -digits 3+ digits not dates
odds_hits = []
for m in re.finditer(r"(?<![0-9T])([+-])(\d{3,4})(?![0-9])", text):
    val = m.group(0)
    start = max(0, m.start() - 100)
    end = min(len(text), m.end() + 100)
    snippet = text[start:end].replace("\n", " ")
    odds_hits.append((val, snippet))

print("hits", len(odds_hits))
for val, snip in odds_hits[:25]:
    print(val, snip[:180])

# search for prop section markers
for marker in ["FIGHT PROPS", "Method of Victory", "Goes To Distance", "Round", "Total Rounds", "Fight Ends"]:
    i = text.find(marker)
    print(marker, i)
