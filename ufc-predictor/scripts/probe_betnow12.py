import re
import requests

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
for url in [
    "https://www.betnow.eu/sportsbook-info/fighting/ufc/",
    "https://www.betnow.eu/live-betting/",
    "https://www.betnow.eu/sportsbook-info/",
]:
    r = requests.get(url, headers=headers, timeout=30)
    text = r.text
    print("===", url, r.status_code)
    json_urls = sorted(set(re.findall(r'["\'](/[^"\']+\.json[^"\']*)["\']', text)))
    json_urls += sorted(set(re.findall(r'https?://[^"\']+\.json[^"\']*', text)))
    for u in json_urls[:20]:
        print("json", u[:120])
    for pat in [r"/[a-zA-Z0-9_./-]*odds[a-zA-Z0-9_./-]*", r"/[a-zA-Z0-9_./-]*lines[a-zA-Z0-9_./-]*"]:
        hits = sorted(set(re.findall(pat, text, re.I)))
        for h in hits:
            if "wp-content" not in h and "cache" not in h and len(h) < 80:
                print("path", h)
