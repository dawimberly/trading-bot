import re
import requests
from bs4 import BeautifulSoup

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
for page in [
    "https://www.betnow.eu/",
    "https://www.betnow.eu/sports-betting/",
    "https://www.betnow.eu/nfl/",
]:
    r = requests.get(page, headers=headers, timeout=30)
    soup = BeautifulSoup(r.text, "lxml")
    print("===", page, r.status_code)
    for iframe in soup.find_all("iframe"):
        print("iframe", iframe.get("src"), iframe.get("id"), iframe.get("name"))
    for el in soup.find_all(attrs={"data-src": True}):
        ds = el.get("data-src", "")
        if any(x in ds.lower() for x in ["sport", "line", "odds", "wager", "bet"]):
            print("data-src", ds[:120])
    # inline odds widget scripts
    for s in soup.find_all("script"):
        txt = s.string or ""
        if "odds" in txt.lower() or "lineserver" in txt.lower() or "sportsbook" in txt.lower():
            if len(txt) > 50:
                print("script snippet", txt[:300].replace("\n", " "))
