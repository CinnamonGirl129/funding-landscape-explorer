"""
Funding Landscape Explorer - MVP backend.

This is the "librarian": it holds one persistent DuckDB connection, loads a
trimmed, indexed copy of the real QWNTL Open Grant Data on startup (funders,
recipients, and the funder-to-recipient relationship edges), and answers a
small, fixed set of questions over HTTP. It never invents a relationship: every
edge it returns traces to a row in QWNTL's graph_edges table.

Deliberately out of scope for this MVP (see the v0.3/v0.4 discussion this
came out of): full-text semantic search over embeddings, grant opportunities,
time scrubbing, NST scoring, national-scale clustering. Those are separate,
later additions on top of this same data layer, not blockers to it.
"""

import logging
import time
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("funding-explorer")

# The real, published QWNTL parquet files (CC0). Column names below were
# confirmed against the Hugging Face dataset-server schema for this dataset,
# not guessed.
BASE = "https://huggingface.co/datasets/qwntl-labs/open-grant-data/resolve/refs%2Fconvert%2Fparquet"
FUNDERS_GLOB = f"{BASE}/funders/train/*.parquet"
RECIPIENTS_GLOB = f"{BASE}/recipients/train/*.parquet"
EDGES_GLOB = f"{BASE}/graph_edges/train/*.parquet"

DB_PATH = Path(__file__).parent / "explorer.duckdb"

app = FastAPI(title="Funding Landscape Explorer API")

_con: duckdb.DuckDBPyConnection | None = None


def get_con() -> duckdb.DuckDBPyConnection:
    if _con is None:
        raise HTTPException(503, "Data is still loading, try again in a few seconds.")
    return _con


def build_database() -> duckdb.DuckDBPyConnection:
    """
    One-time ETL: pull only the columns this app actually needs (never the
    1024-dim embeddings, which would balloon memory for no benefit here) from
    the real remote parquet into a small local DuckDB file, then index it.
    This runs once at container startup. Everything after that is a fast
    local, indexed lookup, not a remote re-scan.
    """
    log.info("Loading real QWNTL data from Hugging Face, this happens once...")
    t0 = time.time()

    con = duckdb.connect(str(DB_PATH))
    con.execute("INSTALL httpfs; LOAD httpfs;")

    con.execute(f"""
        CREATE OR REPLACE TABLE funders AS
        SELECT
            id AS funder_id,
            ein,
            organization_name,
            funder_type,
            city,
            state_code,
            grant_size_minimum,
            grant_size_maximum
        FROM read_parquet('{FUNDERS_GLOB}')
    """)

    con.execute(f"""
        CREATE OR REPLACE TABLE recipients AS
        SELECT
            recipient_id,
            name,
            ein,
            state,
            ntee_code,
            mission
        FROM read_parquet('{RECIPIENTS_GLOB}')
    """)

    con.execute(f"""
        CREATE OR REPLACE TABLE edges AS
        SELECT
            funder_id,
            recipient_id,
            TRY_CAST(year AS INTEGER) AS year,
            amount
        FROM read_parquet('{EDGES_GLOB}')
    """)

    log.info("Building indexes...")
    con.execute("CREATE INDEX idx_edges_funder ON edges(funder_id)")
    con.execute("CREATE INDEX idx_edges_recipient ON edges(recipient_id)")
    con.execute("CREATE INDEX idx_funders_id ON funders(funder_id)")
    con.execute("CREATE INDEX idx_recipients_id ON recipients(recipient_id)")

    counts = con.execute("""
        SELECT
            (SELECT count(*) FROM funders),
            (SELECT count(*) FROM recipients),
            (SELECT count(*) FROM edges)
    """).fetchone()
    log.info(
        "Loaded %s funders, %s recipients, %s edges in %.1fs",
        *counts, time.time() - t0,
    )
    return con


@app.on_event("startup")
def startup():
    global _con
    _con = build_database()


# ---- API ---------------------------------------------------------------

@app.get("/api/search")
def search(q: str = Query(..., min_length=2), limit: int = 15):
    """Search recipients and funders by name. Used to enter the graph."""
    con = get_con()
    like = f"%{q}%"

    recipients = con.execute(
        """
        SELECT recipient_id AS id, name, state, 'recipient' AS kind
        FROM recipients
        WHERE name ILIKE ?
        LIMIT ?
        """,
        [like, limit],
    ).fetchall()

    funders = con.execute(
        """
        SELECT funder_id AS id, organization_name AS name, state_code AS state, 'funder' AS kind
        FROM funders
        WHERE organization_name ILIKE ?
        LIMIT ?
        """,
        [like, limit],
    ).fetchall()

    cols = ["id", "name", "state", "kind"]
    results = [dict(zip(cols, row)) for row in recipients + funders]
    return {"query": q, "results": results}


@app.get("/api/recipient/{recipient_id}/funders")
def recipient_funders(recipient_id: str, limit: int = 40):
    """
    Given a recipient, return its real documented funders, aggregated across
    years. Every row here traces directly to one or more edges rows.
    """
    con = get_con()

    recipient = con.execute(
        "SELECT recipient_id, name, state, ntee_code, mission FROM recipients WHERE recipient_id = ?",
        [recipient_id],
    ).fetchone()
    if recipient is None:
        raise HTTPException(404, "Unknown recipient_id")

    rows = con.execute(
        """
        SELECT
            f.funder_id,
            f.organization_name,
            f.funder_type,
            f.city,
            f.state_code,
            count(*) AS grant_count,
            sum(e.amount) AS total_amount,
            min(e.year) AS first_year,
            max(e.year) AS last_year
        FROM edges e
        JOIN funders f ON f.funder_id = e.funder_id
        WHERE e.recipient_id = ?
        GROUP BY f.funder_id, f.organization_name, f.funder_type, f.city, f.state_code
        ORDER BY total_amount DESC
        LIMIT ?
        """,
        [recipient_id, limit],
    ).fetchall()

    cols = [
        "funder_id", "name", "funder_type", "city", "state",
        "grant_count", "total_amount", "first_year", "last_year",
    ]
    return {
        "recipient": {
            "id": recipient[0], "name": recipient[1], "state": recipient[2],
            "ntee_code": recipient[3], "mission": recipient[4],
        },
        "funders": [dict(zip(cols, row)) for row in rows],
    }


@app.get("/api/funder/{funder_id}/recipients")
def funder_recipients(funder_id: str, limit: int = 40):
    """Given a funder, return its real documented recipient ecosystem."""
    con = get_con()

    funder = con.execute(
        "SELECT funder_id, organization_name, funder_type, city, state_code FROM funders WHERE funder_id = ?",
        [funder_id],
    ).fetchone()
    if funder is None:
        raise HTTPException(404, "Unknown funder_id")

    rows = con.execute(
        """
        SELECT
            r.recipient_id,
            r.name,
            r.state,
            r.ntee_code,
            count(*) AS grant_count,
            sum(e.amount) AS total_amount,
            min(e.year) AS first_year,
            max(e.year) AS last_year
        FROM edges e
        JOIN recipients r ON r.recipient_id = e.recipient_id
        WHERE e.funder_id = ?
        GROUP BY r.recipient_id, r.name, r.state, r.ntee_code
        ORDER BY total_amount DESC
        LIMIT ?
        """,
        [funder_id, limit],
    ).fetchall()

    cols = [
        "recipient_id", "name", "state", "ntee_code",
        "grant_count", "total_amount", "first_year", "last_year",
    ]
    return {
        "funder": {
            "id": funder[0], "name": funder[1], "type": funder[2],
            "city": funder[3], "state": funder[4],
        },
        "recipients": [dict(zip(cols, row)) for row in rows],
    }


@app.get("/api/evidence")
def evidence(funder_id: str, recipient_id: str):
    """
    The provenance panel. Given one selected relationship (one edge on
    screen), return every underlying source row it was built from: individual
    grant-year records, not the aggregate.
    """
    con = get_con()
    rows = con.execute(
        """
        SELECT year, amount
        FROM edges
        WHERE funder_id = ? AND recipient_id = ?
        ORDER BY year
        """,
        [funder_id, recipient_id],
    ).fetchall()
    if not rows:
        raise HTTPException(404, "No documented relationship between these two records")
    return {
        "funder_id": funder_id,
        "recipient_id": recipient_id,
        "records": [{"year": r[0], "amount": r[1]} for r in rows],
        "source": "QWNTL Labs Open Grant Data (CC0)",
    }


@app.get("/api/status")
def status():
    con = get_con()
    counts = con.execute(
        "SELECT (SELECT count(*) FROM funders), (SELECT count(*) FROM recipients), (SELECT count(*) FROM edges)"
    ).fetchone()
    return {"funders": counts[0], "recipients": counts[1], "edges": counts[2]}


# ---- Static frontend -----------------------------------------------------
# Single self-contained HTML file (all CSS/JS inline, D3 and fonts from CDN),
# so it's served directly from the repo root, no separate static folder.

@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")
