"""
将 benchmark_dataset.json 导入 eval.db
"""
import sys
sys.path.insert(0, ".")

import json
from pathlib import Path

from eval.storage.eval_db import get_eval_storage

ROOT = Path(__file__).parent

def main():
    storage = get_eval_storage()
    dataset_path = ROOT / "benchmark_dataset.json"
    with open(dataset_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    cases = data.get("cases", [])
    imported = 0
    for case in cases:
        storage.upsert_benchmark_case(case)
        imported += 1
    storage.close()
    print(f"[OK] 导入 {imported} 条 benchmark case 到 eval.db")

if __name__ == "__main__":
    main()
