import glob, re, os
NEW = "skills"
OLD = "skills-en-backup"
SEP = chr(92)  # backslash

def names(root):
    d = {}
    for p in glob.glob(root + "/**/SKILL.md", recursive=True):
        rel = os.path.relpath(p, root).replace(SEP, "/")
        t = open(p, encoding="utf-8").read()
        m = re.search(r"^name:\s*(\S+)", t, re.M)
        d[rel] = m.group(1) if m else None
    return d

n = names(NEW)
o = names(OLD)
mismatch = [k for k in o if n.get(k) != o[k]]
missing = [k for k in o if k not in n]
none_new = [k for k, v in n.items() if v is None]

print("SKILL.md count:", len(o))
print("name field mismatches (new != old):", len(mismatch))
for k in mismatch:
    print("   name changed:", k, "|", o[k], "->", n.get(k))
print("missing in new:", len(missing))
for k in missing:
    print("   -", k)
print("name field parsed as None in new:", len(none_new))
for k in none_new:
    print("   ??", k)
print("\nRESULT:", "ALL name: IDENTIFIERS PRESERVED" if not (mismatch or missing or none_new) else "ISSUES FOUND")
