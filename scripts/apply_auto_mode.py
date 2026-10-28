import json
import shutil
import sys

path = sys.argv[1]
backup = path + ".bak-doctor-20261018"
shutil.copy2(path, backup)

with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)

if not isinstance(data, dict):
    raise SystemExit("settings root is not an object")

permissions = data.get("permissions")
if not isinstance(permissions, dict):
    permissions = {}
permissions["defaultMode"] = "auto"
data["permissions"] = permissions

with open(path, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")

print(f"applied (backup: {backup})")
