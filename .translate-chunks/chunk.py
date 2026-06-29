import os, json, glob

ROOT = "C:/Users/hapii/Documents/GitHub/hermes-agent/skills"
OUTDIR = "C:/Users/hapii/Documents/GitHub/hermes-agent/.translate-chunks"
os.makedirs(OUTDIR, exist_ok=True)

# known leaf dirs that are NOT the skill root themselves
LEAF = ("/references", "/templates", "/layouts", "/styles", "/tests", "/workflows")

def skill_root(rel):
    # rel like creative/ascii-video/references/effects.md
    low = rel.lower()
    for leaf in LEAF:
        idx = low.find(leaf + "/")
        if idx != -1:
            return rel[:idx]
    # drop a trailing file, group by its dir
    return os.path.dirname(rel)

files = []
for p in glob.glob(ROOT + "/**/*.md", recursive=True):
    rel = os.path.relpath(p, ROOT).replace("\\", "/")
    files.append((rel, os.path.getsize(p)))

# group by skill root
groups = {}
for rel, sz in files:
    g = skill_root(rel)
    groups.setdefault(g, []).append((rel, sz))

def gsize(items):
    return sum(s for _, s in items)

# split oversized groups into sub-chunks (<= cap) by file size desc
CAP = 130_000
SUBCAP = 125_000
subgroups = []  # list of (root, [(rel,sz)])
for g, items in sorted(groups.items()):
    if gsize(items) <= CAP:
        subgroups.append((g, items))
    else:
        items_sorted = sorted(items, key=lambda x: -x[1])
        cur = []
        cursz = 0
        for rel, sz in items_sorted:
            if cur and cursz + sz > SUBCAP:
                subgroups.append((g, cur))
                cur, cursz = [], 0
            cur.append((rel, sz))
            cursz += sz
        if cur:
            subgroups.append((g, cur))

# greedy first-fit-decreasing bin pack into target chunks
TARGET = 100_000
MAXCHUNK = 130_000
sub_sorted = sorted(subgroups, key=lambda sg: -gsize(sg[1]))
chunks = []  # each: {"roots":[], "files":[], "size":n}
for g, items in sub_sorted:
    sz = gsize(items)
    placed = False
    for c in chunks:
        if c["size"] + sz <= MAXCHUNK and len(c["files"]) + len(items) <= 8:
            c["roots"].append(g)
            c["files"].extend(rel for rel, _ in items)
            c["size"] += sz
            placed = True
            break
    if not placed:
        chunks.append({"roots": [g], "files": [rel for rel, _ in items], "size": sz})

# sort files within each chunk for determinism, reindex
result = []
for i, c in enumerate(sorted(chunks, key=lambda c: c["files"][0]), 1):
    c["files"].sort()
    result.append({
        "id": i,
        "roots": sorted(set(c["roots"])),
        "files": c["files"],
        "sizeKB": round(c["size"] / 1024),
        "count": len(c["files"]),
    })

with open(os.path.join(OUTDIR, "chunks.json"), "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=1)

# print summary table
total_files = sum(c["count"] for c in result)
total_kb = sum(c["sizeKB"] for c in result)
print(f"CHUNKS: {len(result)}  |  FILES: {total_files}  |  TOTAL: {total_kb} KB")
print("-" * 70)
for c in result:
    roots = ", ".join(c["roots"])
    if len(roots) > 48:
        roots = roots[:45] + "..."
    print(f"#{c['id']:>2}  {c['count']:>2} files  {c['sizeKB']:>4} KB  {roots}")
