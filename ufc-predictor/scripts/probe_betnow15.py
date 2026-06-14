import re
import requests
from bs4 import BeautifulSoup

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
r = requests.get("https://www.betnow.eu/sportsbook-info/football/nfl/", headers=headers, timeout=30)
soup = BeautifulSoup(r.text, "lxml")
odds = soup.select_one("#odds")
rows = odds.select(".odd-info-teams")[:6] if odds else []
print("nfl rows", len(rows))
for row in rows:
    txt = row.get_text(" | ", strip=True)
    html = str(row)
    am = re.findall(r"(?<![0-9T])([+-]\d{3,4})(?![0-9])", html)
    print(txt[:120], "odds", am)
