import re
import requests
from bs4 import BeautifulSoup

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
url = "https://www.betnow.eu/sportsbook-info/fighting/ufc/24013"
r = requests.get(url, headers=headers, timeout=30)
text = r.text
soup = BeautifulSoup(text, "lxml")
print("status", r.status_code, "len", len(text))
print("title snippet", soup.title.string if soup.title else "")

for kw in [
    "Method",
    "Decision",
    "Submission",
    "KO",
    "Round",
    "Goes",
    "Total",
    "Over",
    "Under",
    "Chandler",
    "Ruffy",
    "Money Line",
]:
    print(kw, text.count(kw))

odds = soup.select_one("#odds") or soup
body_text = odds.get_text("\n", strip=True)
print("--- body sample ---")
print(body_text[:4000])

# extract lines with odds
for line in body_text.split("\n"):
    if re.search(r"[+-]\d{2,4}", line) or any(
        k in line.lower() for k in ["decision", "submission", "ko", "round", "over", "under", "method"]
    ):
        if len(line) < 200:
            print("LINE", line)
