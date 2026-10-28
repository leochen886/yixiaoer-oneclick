import re
import sys

path = sys.argv[1]
terms = [t.lower() for t in sys.argv[2:]]
data = open(path, "rb").read()
seen = set()
for match in re.finditer(rb"[\x20-\x7e]{5,}", data):
    text = match.group().decode("latin1")
    low = text.lower()
    if any(term in low for term in terms):
        if text not in seen:
            seen.add(text)
            print(text[:300])
