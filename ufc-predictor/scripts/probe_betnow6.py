import re
import requests
from bs4 import BeautifulSoup

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
r = requests.get("https://www.betnow.eu/sportsbook-info/fighting/ufc/", headers=headers, timeout=30)
soup = BeautifulSoup(r.text, "lxml")
odds = soup.select_one("#odds")
html = str(odds) if odds else ""
print("html len", len(html))
# rotation blocks
rots = re.findall(r"\b24\d{3}\b", html)
print("rotations", sorted(set(rots))[:20], "count", len(set(rots)))
# elements near Chandler
idx = html.find("Chandler")
if idx >= 0:
    print("chandler context", html[max(0, idx - 400) : idx + 800])

# search for odds in attributes
for pat in [r"data-[a-z-]+=\"[^\"]+\""]:
    hits = re.findall(pat, html)
    print(pat, len(hits))
    for h in hits[:10]:
        if "24" in h or "+" in h or "-" in h:
            print(" ", h[:120])

# script tags on main page with lines/odds
for s in soup.find_all("script"):
    txt = s.string or ""
    if "24013" in txt or "Chandler" in txt or "getOdds" in txt or "lines" in txt.lower():
        print("script hit", txt[:500].replace("\n", " "))
