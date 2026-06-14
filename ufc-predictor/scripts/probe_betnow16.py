import requests
from bs4 import BeautifulSoup

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
r = requests.get("https://www.betnow.eu/sportsbook-info/football/nfl/", headers=headers, timeout=30)
soup = BeautifulSoup(r.text, "lxml")
row = soup.select_one(".odd-info-teams")
print(row.prettify()[:2500])
