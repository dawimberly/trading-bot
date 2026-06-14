import re
import requests
from bs4 import BeautifulSoup

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
r = requests.get("https://www.betnow.eu/sportsbook-info/fighting/ufc/", headers=headers, timeout=30)
text = r.text
print("american odds count", len(re.findall(r"[+-]\d{3,4}", text)))
for kw in ["Method", "Decision", "Submission", "KO", "Round", "Goes", "Total", "Over", "Under", "prop"]:
    print(kw, text.count(kw))

soup = BeautifulSoup(text, "lxml")
odds = soup.select_one("#odds")
if odds:
    for a in odds.find_all("a", href=True)[:20]:
        print("a", a.get("href")[:100], a.get_text(" ", strip=True)[:80])
    # look for onclick or data attributes with odds
    for el in odds.find_all(True)[:50]:
        attrs = {k: v for k, v in el.attrs.items() if k not in ("class", "style")}
        if attrs and any(str(v) for v in attrs.values() if re.search(r"[+-]\d{3}", str(v))):
            print("el", el.name, attrs)

# try fight detail endpoint patterns
for path in [
    "/sportsbook-info/fighting/ufc/24013",
    "/sportsbook-info/fighting/ufc/?rotation=24013",
    "/sportsbook-info/fighting/ufc/props/",
]:
    url = "https://www.betnow.eu" + path
    rr = requests.get(url, headers=headers, timeout=20)
    print(path, rr.status_code, len(rr.text), rr.text.count("Chandler"), len(re.findall(r"[+-]\d{3,4}", rr.text)))
