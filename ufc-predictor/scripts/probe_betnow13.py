import re
import requests

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
text = requests.get(
    "https://www.betnow.eu/sportsbook-info/fighting/ufc/", headers=headers, timeout=30
).text
scripts = re.findall(r'src=["\']([^"\']+)["\']', text)
for s in scripts:
    low = s.lower()
    if any(k in low for k in ["sport", "odd", "line", "wager", "bet"]):
        print(s)
