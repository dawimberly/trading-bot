"""Probe MyBookie per-fight prop pages."""
import requests
from bs4 import BeautifulSoup

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
r = requests.get("https://www.mybookie.ag/sportsbook/ufc/", headers=headers, timeout=20)
soup = BeautifulSoup(r.text, "lxml")
links = soup.select("a[data-props-count], .game-line__props a")[:3]
print("prop links", len(links))
for a in links:
    href = a.get("href", "")
    print(" ", href, a.get("data-props-count"))

if links:
    href = links[0].get("href", "")
    if href.startswith("/"):
        url = "https://www.mybookie.ag" + href
    elif href.startswith("?"):
        url = "https://www.mybookie.ag/sportsbook/ufc/" + href
    else:
        url = href
    print("fetch", url)
    r2 = requests.get(url, headers=headers, timeout=20)
    s2 = BeautifulSoup(r2.text, "lxml")
    btns = s2.select("button.lines-odds")
    print("buttons", len(btns))
    for b in btns[:20]:
        print(
            b.get("data-markettype"),
            b.get("data-wager-type"),
            b.get("data-team"),
            b.get("data-odd"),
            repr(b.get("data-description", "")[:60]),
            b.get_text(" ", strip=True)[:80],
        )
    # other structures
    for sel in [".prop-line", ".market-name", ".game-line", "h3", ".lines-odds"]:
        els = s2.select(sel)
        if els:
            print(sel, len(els), els[0].get_text(" ", strip=True)[:80])
