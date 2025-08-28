#!/usr/bin/env python3
"""
Read one or more CSV files produced by `psql --csv` from your test SQLs.
Each CSV chunk has at least columns: test, pass, and optionally anything else.
We normalize to: run_id, suite, test, pass, payload(json).

Usage:
  python scripts/ingest_test_csv.py RUN_ID file1.csv [file2.csv ...] > normalized.csv
"""
import csv, json, sys, os

if len(sys.argv) < 3:
    sys.stderr.write("Usage: ingest_test_csv.py RUN_ID file1.csv [file2.csv ...]\n")
    sys.exit(1)

run_id = sys.argv[1]
out = csv.writer(sys.stdout)
# header
out.writerow(["run_id", "suite", "test", "pass", "payload"])

for path in sys.argv[2:]:
    suite = os.path.basename(path)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        headers = None
        for row in reader:
            if not row:
                continue
            # Detect a header row (must contain 'test' and 'pass')
            if "test" in row and "pass" in row:
                headers = row
                continue
            if headers is None:
                # Skip until a header is found
                continue

            record = dict(zip(headers, row))
            # Required fields
            test_name = record.get("test")
            pass_val = record.get("pass")

            # Convert pass to boolean string acceptable by Postgres COPY
            if pass_val is None:
                pass_bool = ""
            else:
                pv = pass_val.strip().lower()
                pass_bool = "true" if pv in ("t", "true", "1") else "false"

            # Build payload from remaining columns (excluding 'test'/'pass')
            payload_dict = {k: v for k, v in record.items() if k not in ("test", "pass")}
            # Try to coerce numeric-looking values
            for k, v in list(payload_dict.items()):
                if v is None or v == "":
                    continue
                vv = v.strip()
                if vv.isdigit():
                    payload_dict[k] = int(vv)
                else:
                    try:
                        payload_dict[k] = float(vv)
                    except ValueError:
                        payload_dict[k] = vv

            out.writerow([run_id, suite, test_name, pass_bool, json.dumps(payload_dict)])
