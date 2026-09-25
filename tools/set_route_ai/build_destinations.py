import json
import re
import os

# -----------------------------
# Paths
# -----------------------------

BASE_PATH = os.path.join(os.path.dirname(__file__), "data")
DEST_FILE = os.path.join(BASE_PATH, "destinations.json")
RAW_FILE = os.path.join(os.path.dirname(__file__), "raw_locations.txt")

# -----------------------------
# Normalization (must match engine logic)
# -----------------------------

def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s\-]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

# -----------------------------
# Ensure /data folder exists
# -----------------------------

os.makedirs(BASE_PATH, exist_ok=True)

# -----------------------------
# Load existing destinations
# -----------------------------

if os.path.exists(DEST_FILE):
    with open(DEST_FILE, "r", encoding="utf-8") as f:
        destinations = json.load(f)
else:
    destinations = {}

original_count = len(destinations)

# -----------------------------
# Load raw location list
# -----------------------------

if not os.path.exists(RAW_FILE):
    print("ERROR: raw_locations.txt not found.")
    print("Create raw_locations.txt in the same folder as this script.")
    exit()

with open(RAW_FILE, "r", encoding="utf-8") as f:
    lines = f.read().splitlines()

added = 0

for line in lines:
    name = line.strip()
    if not name:
        continue

    key = normalize(name)

    if key not in destinations:
        destinations[key] = {"aliases": []}
        added += 1

# -----------------------------
# Sort and save
# -----------------------------

destinations = dict(sorted(destinations.items()))

with open(DEST_FILE, "w", encoding="utf-8") as f:
    json.dump(destinations, f, indent=2)

print("Original entries:", original_count)
print("New entries added:", added)
print("Final total:", len(destinations))
print("Done.")