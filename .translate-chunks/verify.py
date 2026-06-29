import os, glob, unicodedata

NEW = "C:/Users/hapii/Documents/GitHub/hermes-agent/skills"
OLD = "C:/Users/hapii/Documents/GitHub/hermes-agent/skills-en-backup"

def list_md(root):
    s = set()
    for p in glob.glob(root + "/**/*.md", recursive=True):
        s.add(os.path.relpath(p, root).replace("\\", "/"))
    return s

def cjk_count(text):
    n = 0
    for ch in text:
        if '一' <= ch <= '鿿' or '㐀' <= ch <= '䶿':
            n += 1
    return n

def letter_count(text):
    return sum(1 for ch in text if ch.isalpha() and ord(ch) < 128)

new_files = list_md(NEW)
old_files = list_md(OLD)

new_only = sorted(new_files - old_files)
old_only = sorted(old_files - new_files)
common = sorted(new_files & old_files)

untranslated = []
low_cjk = []
for rel in common:
    with open(os.path.join(NEW, rel), encoding="utf-8") as f:
        ntxt = f.read()
    with open(os.path.join(OLD, rel), encoding="utf-8") as f:
        otxt = f.read()
    if ntxt == otxt:
        untranslated.append(rel)
        continue
    cjk = cjk_count(ntxt)
    letters = letter_count(ntxt)
    # suspicious: changed but almost no Chinese relative to english letters
    if letters > 200 and cjk / max(letters, 1) < 0.05:
        low_cjk.append((rel, cjk, letters))

print(f"total common md: {len(common)}")
print(f"new-only (stray/created): {len(new_only)}")
for f in new_only:
    print("   +", f)
print(f"old-only (deleted): {len(old_only)}")
for f in old_only:
    print("   -", f)
print(f"\nUNTRANSLATED (identical to backup): {len(untranslated)}")
for f in untranslated:
    print("   !", f)
print(f"\nLOW_CJK (suspicious, <5% cjk vs ascii letters): {len(low_cjk)}")
for f, c, l in low_cjk:
    print(f"   ? {f}  (cjk={c}, letters={l})")
print("\nSUMMARY:",
      f"untranslated={len(untranslated)}",
      f"low_cjk={len(low_cjk)}",
      f"stray={len(new_only)}",
      f"deleted={len(old_only)}")
