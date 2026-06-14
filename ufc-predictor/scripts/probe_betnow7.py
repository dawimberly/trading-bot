import re
import requests
from bs4 import BeautifulSoup

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
r = requests.get("https://www.betnow.eu/sportsbook-info/fighting/ufc/", headers=headers, timeout=30)
soup = BeautifulSoup(r.text, "lxml")
odds = soup.select_one("#odds")
game = odds.find(id=re.compile(r"game\d+"), string=re.compile("Chandler", re.I)) if odds else None
# find game div containing Chandler
target = None
for g in odds.find_all("div", id=re.compile(r"^game\d+")) if odds else []:
    if "Chandler" in g.get_text():
        target = g
        break
print("game found", target is not None, target.get("id") if target else None)
if target:
    text = target.get_text("\n", strip=True)
    safe = text.encode("ascii", "replace").decode("ascii")
    print(safe[:2500])
    print("--- odds in html ---")
    html = str(target)
    for m in re.finditer(r"[+-]\d{2,4}", html):
        start = max(0, m.start() - 80)
        snippet = html[start : m.end() + 80].replace("\n", " ")
        print(snippet[:200])
    # class names
    classes = sorted({c for el in target.find_all(True) for c in (el.get("class") or [])})
    print("classes sample", classes[:30])
