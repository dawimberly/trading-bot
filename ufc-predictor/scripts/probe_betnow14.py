import re
import requests

t = requests.get(
    "https://www.betnow.eu/assets/js/sportsbook-info.js?v=0.89.05",
    headers={"User-Agent": "Mozilla/5.0"},
    timeout=30,
).text
print("len", len(t))
for pat in ["http", "/api", "Lines", "odds", "prop", "ajax", "fetch", "rotation", "guest", "login", "method", "wager"]:
    print(pat, t.lower().count(pat.lower()))

paths = sorted(set(re.findall(r'["\'](/[a-zA-Z0-9_./-]+)["\']', t)))
for u in paths:
    low = u.lower()
    if any(x in low for x in ["line", "odd", "sport", "wager", "prop", "fight", "ufc", "api"]):
        print(u)

# show snippets with url/fetch/ajax
for m in re.finditer(r".{0,60}(fetch|ajax|getJSON|load)\(.{0,120}", t, re.I):
    print("call", m.group(0)[:200])
