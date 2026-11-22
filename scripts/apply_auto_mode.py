import json
import os
import tempfile
from pathlib import Path
import shutil
import sys

path = sys.argv[1]
backup = path + ".bak-doctor-20261018"

with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)

if not isinstance(data, dict):
    raise SystemExit("settings root is not an object")

permissions = data.get("permissions")
if not isinstance(permissions, dict):
    permissions = {}
permissions["defaultMode"] = "auto"
data["permissions"] = permissions

# Keep the first recovery copy across repeated runs.
try:
    with open(backup, "xb") as recovery, open(path, "rb") as original:
        shutil.copyfileobj(original, recovery)
except FileExistsError:
    pass

with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False,
                                 dir=Path(path).parent) as prepared:
    replacement = Path(prepared.name)
    try:
        json.dump(data, prepared, ensure_ascii=False, indent=2)
        prepared.write("\n")
        prepared.flush()
        os.fsync(prepared.fileno())
    except BaseException:
        prepared.close()
        replacement.unlink(missing_ok=True)
        raise
try:
    shutil.copymode(path, replacement)
    os.replace(replacement, path)
finally:
    replacement.unlink(missing_ok=True)

print(f"applied (backup: {backup})")
