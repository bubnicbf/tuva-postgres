import csv, sys, pathlib

def normalize_csv(path: pathlib.Path):
    rows = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = [h.strip() for h in reader.fieldnames]
        for r in reader:
            rows.append({k.strip(): (v.strip() or "") for k, v in r.items()})
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)

if __name__ == "__main__":
    folder = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "data")
    for p in folder.glob("*.csv"):
        normalize_csv(p)
        print(f"Normalized {p}")
