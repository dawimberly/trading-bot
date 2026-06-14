import json
import re
import requests

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
r = requests.get("https://www.betnow.eu/ufc/", headers=headers, timeout=30)
text = r.text
print("len", len(text))

# common API patterns
patterns = [
    r"fetch\(['\"]([^'\"]+)['\"]",
    r"axios\.[a-z]+\(['\"]([^'\"]+)['\"]",
    r"src=['\"]([^'\"]+\.js)['\"]",
    r"/api/[a-zA-Z0-9_./-]+",
    r"ws[s]?://[^\"'\s]+",
]
for pat in patterns:
    hits = re.findall(pat, text)
    if hits:
        print("pat", pat[:40], "n", len(hits))
        for h in sorted(set(hits))[:15]:
            print(" ", h[:120])

# look for fighter names from user image
for name in ["Chandler", "Topuria", "Pereira", "Nickal", "Gaethje"]:
    print(name, name in text)

# script tags with substantial content
from bs4 import BeautifulSoup

soup = BeautifulSoup(text, "lxml")
for s in soup.find_all("script"):
    content = s.string or ""
    if len(content) < 100:
        continue
    if any(k in content.lower() for k in ["odds", "line", "fight", "ufc", "market", "prop"]):
        print("script", len(content), content[:200].replace("\n", " "))
