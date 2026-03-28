from pathlib import Path

import pandas as pd


def create_table(data: list[dict], candidate_name: str, jd_name: str, output_dir: str | Path) -> pd.DataFrame:
    df = pd.DataFrame(data)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    safe_c = "".join(c if c.isalnum() or c in "._-" else "_" for c in candidate_name)[:80]
    safe_j = "".join(c if c.isalnum() or c in "._-" else "_" for c in jd_name)[:80]
    output_path = out / f"{safe_c}__{safe_j}.csv"
    df.to_csv(output_path, index=False)
    return df
