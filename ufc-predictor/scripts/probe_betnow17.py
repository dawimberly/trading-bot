import re
import requests
from bs4 import BeautifulSoup

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
r = requests.get("https://www.betnow.eu/sportsbook-info/football/nfl/", headers=headers, timeout=30)
soup = BeautifulSoup(r.text, "lxml")
for row in soup.select(".odd-info-teams")[:3]:
    print("ROW", row.get_text(" | ", strip=True))
    for span in row.find_all("span"):
        attrs = {k: v for k, v in span.attrs.items() if k != "class"}
        if attrs:
            print(" span", attrs)
    for img in row.find_all("img"):
        print(" img", {k: img.get(k) for k in ["alt", "title", "src"] if img.get(k)})
