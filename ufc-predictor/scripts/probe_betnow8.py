import re
import requests
from bs4 import BeautifulSoup

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
r = requests.get("https://www.betnow.eu/sportsbook-info/fighting/ufc/", headers=headers, timeout=30)
soup = BeautifulSoup(r.text, "lxml")
odds = soup.select_one("#odds")
games = odds.find_all("div", id=re.compile(r"^game\d+")) if odds else []
print("games", len(games))
for g in games:
    txt = g.get_text(" ", strip=True)
    if "24013" in txt or "Chandler" in txt or "Topuria" in txt:
        print("id", g.get("id"), txt[:300])
        html = str(g)
        am = re.findall(r"[+-]\d{2,4}", html)
        print("american", am[:20])
        # print structure of odd rows
        for row in g.select(".odd-row, .odds-row, tr, .line, .market"):
            t = row.get_text(" | ", strip=True)
            if t:
                print("row", t[:150])
        break
else:
    # print first game with odds
    for g in games[:5]:
        am = re.findall(r"[+-]\d{2,4}", str(g))
        print(g.get("id"), g.get_text(" ", strip=True)[:120], "odds", am[:6])
