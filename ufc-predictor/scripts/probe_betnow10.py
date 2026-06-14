import re
import requests
from bs4 import BeautifulSoup

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
r = requests.get("https://www.betnow.eu/sportsbook-info/fighting/ufc/", headers=headers, timeout=30)
soup = BeautifulSoup(r.text, "lxml")
g = soup.find("div", id="game936")
print("game936 found", g is not None)
if g:
    print(g.prettify()[:6000])
