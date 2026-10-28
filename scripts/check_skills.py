import sys

import yaml

for path in sys.argv[1:]:
    try:
        text = open(path, "r", encoding="utf-8").read()
    except Exception as exc:
        print(f"{path}\tREAD_ERROR\t{exc}")
        continue
    if not text.startswith("---"):
        print(f"{path}\tNO_FRONTMATTER")
        continue
    parts = text.split("---", 2)
    if len(parts) < 3:
        print(f"{path}\tUNCLOSED_FRONTMATTER")
        continue
    try:
        data = yaml.safe_load(parts[1]) or {}
    except Exception as exc:
        print(f"{path}\tYAML_ERROR\t{exc}")
        continue
    if not isinstance(data, dict):
        print(f"{path}\tNOT_A_MAPPING")
        continue
    name = data.get("name", "")
    desc = data.get("description", "")
    print(
        f"{path}\tname={name}\tdescription_chars={len(str(desc))}\tkeys={','.join(map(str, data.keys()))}"
    )
