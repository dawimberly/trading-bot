import re
import requests

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
r = requests.get("https://www.betnow.eu/", headers=headers, timeout=30)
text = r.text
print("status", r.status_code, "len", len(text))
for pat in ["mma", "ufc", "fighting", "api", "method", "decision", "over", "under"]:
    print(pat, len(re.findall(pat, text, re.I)))

paths = set(re.findall(r'["\'](/[^"\']+)["\']', text))
for p in sorted(paths):
    low = p.lower()
    if any(x in low for x in ["sport", "mma", "ufc", "fight", "line", "api", "odds"]):
        print("path", p[:120])

urls = set(re.findall(r"https?://[^\s\"'<>]+", text))
for u in sorted(urls):
    low = u.lower()
    if any(x in low for x in ["api", "sport", "mma", "ufc", "fight", "odds"]):
        print("url", u[:140])
