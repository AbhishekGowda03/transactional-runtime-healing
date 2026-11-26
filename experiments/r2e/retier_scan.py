"""Fill in the Python version and tier of scan records that have none.

While an image was still being pulled, the scan could not ask it for its interpreter and wrote
python=null, tier="unknown"; eval_r2e.py then never selects such a task. This asks the images that are
now on this machine (nothing is pulled) and rewrites the scan file; the old file is kept as .bak.

    uv run python experiments/r2e/retier_scan.py results/r2e/scan_py37.jsonl
"""
import argparse
import json
import shutil
import subprocess

import scan_r2e


def local_images():
    out = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                         capture_output=True, text=True).stdout
    return [x for x in out.split() if "_final:" in x]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("scan")
    a = ap.parse_args()
    rows = [json.loads(x) for x in open(a.scan) if x.strip()]
    images = local_images()
    changed = missing = 0
    for r in rows:
        if r.get("python") and r.get("tier") not in (None, "unknown"):
            continue
        if not r.get("usable"):
            continue
        match = [i for i in images if i.split("/")[-1].startswith(f"{r['repo']}_final:{r['commit']}")]
        if len(match) != 1:
            print(f"{r['repo']} {r['commit']}: image not on this machine yet, left as it was")
            missing += 1
            continue
        py = scan_r2e.python_version(match[0])
        if not py:
            print(f"{r['repo']} {r['commit']}: the image did not answer, left as it was")
            missing += 1
            continue
        r["python"], r["tier"] = list(py), scan_r2e.tier_of(py)
        changed += 1
        print(f"{r['repo']} {r['commit']}: Python {py[0]}.{py[1]} -> {r['tier']}")
    if changed:
        shutil.copy(a.scan, a.scan + ".bak")
        with open(a.scan, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    print(f"{changed} records updated, {missing} still open")


if __name__ == "__main__":
    main()