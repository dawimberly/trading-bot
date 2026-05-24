import sqlite3
conn = sqlite3.connect('market_data.db')
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
all_tables = [t[0] for t in cursor.fetchall()]
junk = [t for t in all_tables if '_5m' in t or '_daily' in t]
for t in junk:
    cursor.execute("DROP TABLE IF EXISTS '" + t + "'")
    print('Dropped: ' + t)
conn.commit()
conn.close()
print('Database cleaned.')
