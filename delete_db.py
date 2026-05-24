import os

db_path = 'market_data.db'
if os.path.exists(db_path):
    os.remove(db_path)
    print("Database deleted. Please run your fetch_data.py to repopulate it.")
else:
    print("Database not found.")