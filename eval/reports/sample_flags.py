"""Pick a reproducible random sample of flagged clean cases and print each claim with its code."""
import json
import random
import sys

sys.path.insert(0, ".")
from eval import metrics as M

SEED = 20260917
rep = json.load(open("eval/reports/v1.json", encoding="utf-8"))
cases = {c["id"]: c for c in (json.loads(l) for l in open("eval/data/v1.jsonl", encoding="utf-8"))}

flagged = sorted(c["id"] for c in rep["cases"]
                 if c["split"] == "clean" and not M.is_infrastructure_failure(c)
                 and any(M.is_medium_plus(f) for f in c["findings"]))
rng = random.Random(SEED)
sample = sorted(rng.sample(flagged, min(15, len(flagged))))
print(f"seed={SEED} flagged={len(flagged)} sampled={len(sample)}")
print(json.dumps(sample, indent=1))

if len(sys.argv) > 1 and sys.argv[1] == "--detail":
    by_id = {c["id"]: c for c in rep["cases"]}
    for cid in sample:
        case, res = cases[cid], by_id[cid]
        print("#" * 110)
        print(f"{cid} | {case['repo']} PR {case['pr_number']} | {case['title']}")
        print(f"source commit {case['source']['commit']}")
        for f in res["findings"]:
            if not M.is_medium_plus(f):
                continue
            print(f"\n== [{f['severity']}] {f['file']} {f['lines']}: {f['title']}")
            print("DETAIL:", f["detail"][:900])
            src = next((x for x in case["files"] if x["filename"] == f["file"]), None)
            if src and src.get("content"):
                lines = src["content"].splitlines()
                s, e = f["lines"]["start"], f["lines"]["end"]
                lo, hi = max(1, s - 6), min(len(lines), e + 6)
                for n in range(lo, hi + 1):
                    print(f"{'>>' if s <= n <= e else '  '}{n:5d} | {lines[n - 1][:150]}")
            if src:
                print(f"-- patch ({len(src['patch'].splitlines())} lines):")
                print(src["patch"][:2500])
