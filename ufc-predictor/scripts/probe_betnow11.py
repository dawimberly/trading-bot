import re
import requests
from bs4 import BeautifulSoup

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
r = requests.get("https://www.betnow.eu/sportsbook-info/fighting/ufc/", headers=headers, timeout=30)
soup = BeautifulSoup(r.text, "lxml")
g = soup.find("div", id="game936")
if not g:
    raise SystemExit("no game936")
# walk next siblings
node = g
for i in range(15):
    node = node.find_next_sibling()
    if node is None:
        break
    txt = node.get_text(" ", strip=True)
    html = str(node)[:500]
    print(f"--- sibling {i} tag={node.name} id={node.get('id')} class={node.get('class')}")
    print("text", txt[:200])
    print("html", html)
    am = re.findall(r"(?<![0-9T])([+-]\d{3,4})(?![0-9])", str(node))
    if am:
        print("american", am)
