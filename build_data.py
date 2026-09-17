"""
Build-time ETL: run once, during the Docker image build, never at request
time or container startup.

Downloads the real QWNTL Open Grant Data parquet files with a plain HTTP
client (not DuckDB's remote-file reader, which hit two different snags
against Hugging Face's server), then loads the columns this app actually
needs into a small local DuckDB file with indexes. The finished
explorer.duckdb is what ships in the image; the raw downloads are discarded
in the same build stage and never reach the final image.

This also means a container that spins down on Render's free tier and wakes
back up does not have to re-download and re-process 2.8GB before it can
answer a single request. It just opens an existing file.
"""

import logging
import time
import urllib.request
from pathlib import Path

import duckdb

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("build-data")

BASE = "https://huggingface.co/datasets/qwntl-labs/open-grant-data/resolve/refs%2Fconvert%2Fparquet"
RAW_DIR = Path("/tmp/qwntl-raw")
DB_PATH = Path(__file__).parent / "explorer.duckdb"

CONFIGS = {
    "funders": 8,
    "recipients": 4,
    "graph_edges": 16,
}


def download_all() -> dict[str, list[Path]]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    local_paths: dict[str, list[Path]] = {}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; LWG-FundingExplorer/0.4; +https://github.com/CinnamonGirl129/funding-landscape-explorer)"}

    for config, n in CONFIGS.items():
        paths = []
        for i in range(n):
            url = f"{BASE}/{config}/train/{i:04d}.parquet"
            dest = RAW_DIR / f"{config}-{i:04d}.parquet"
            log.info("Downloading %s ...", url)
            req = urllib.request.Request(url, headers=headers)
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            log.info("  -> %s (%.1fs, %.1f MB)", dest.name, time.time() - t0, dest.stat().st_size / 1e6)
            paths.append(dest)
        local_paths[config] = paths
    return local_paths


def build_database(local_paths: dict[str, list[Path]]) -> None:
    log.info("Building indexed local database...")
    t0 = time.time()

    con = duckdb.connect(str(DB_PATH))

    def sql_list(paths: list[Path]) -> str:
        return "[" + ", ".join(f"'{p}'" for p in paths) + "]"

    con.execute(f"""
        CREATE OR REPLACE TABLE funders AS
        SELECT
            id AS funder_id, ein, organization_name, funder_type,
            city, state_code, grant_size_minimum, grant_size_maximum
        FROM read_parquet({sql_list(local_paths["funders"])})
    """)

    con.execute(f"""
        CREATE OR REPLACE TABLE recipients AS
        SELECT recipient_id, name, ein, state, ntee_code, mission
        FROM read_parquet({sql_list(local_paths["recipients"])})
    """)

    con.execute(f"""
        CREATE OR REPLACE TABLE edges AS
        SELECT funder_id, recipient_id, TRY_CAST(year AS INTEGER) AS year, amount
        FROM read_parquet({sql_list(local_paths["graph_edges"])})
    """)

    log.info("Building indexes...")
    con.execute("CREATE INDEX idx_edges_funder ON edges(funder_id)")
    con.execute("CREATE INDEX idx_edges_recipient ON edges(recipient_id)")
    con.execute("CREATE INDEX idx_funders_id ON funders(funder_id)")
    con.execute("CREATE INDEX idx_recipients_id ON recipients(recipient_id)")

    counts = con.execute("""
        SELECT (SELECT count(*) FROM funders),
               (SELECT count(*) FROM recipients),
               (SELECT count(*) FROM edges)
    """).fetchone()
    con.close()
    log.info("Done: %s funders, %s recipients, %s edges in %.1fs total", *counts, time.time() - t0)


if __name__ == "__main__":
    paths = download_all()
    build_database(paths)
