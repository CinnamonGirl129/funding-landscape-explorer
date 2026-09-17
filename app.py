"""
Funding Landscape Explorer - MVP backend.

This is the "librarian": it holds one persistent DuckDB connection over a
trimmed, indexed copy of the real QWNTL Open Grant Data (funders, recipients,
and the funder-to-recipient relationship edges), and answers a small, fixed
set of questions over HTTP. It never invents a relationship: every edge it
returns traces to a row in QWNTL's graph_edges table.

The database itself (explorer.duckdb) is built once at Docker image build
time by build_data.py, not here, and not at container startup. This file
only opens it. See build_data.py for why: fetching 2.8GB of real data live,
on every boot, over an unreliable/blocked-in-testing network path, was the
wrong place to put that risk, especially on a free host whose container
sleeps and wakes repeatedly.

Deliberately out of scope for this MVP (see the v0.3/v0.4 discussion this
came out of): full-text semantic search over embeddings, grant opportunities,
time scrubbing, NST scoring, national-scale clustering. Those are separate,
later additions on top of this same data layer, not blockers to it.
"""

import logging
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("funding-explorer")

DB_PATH = Path(__file__).parent / "explorer.duckdb"

app = FastAPI(title="Funding Landscape Explorer API")

_con: duckdb.DuckDBPyConnection | None = None


def get_con() -> duckdb.DuckDBPyConnection:
    if _con is None:
        raise HTTPException(503, "Data is not available. The build may not have completed.")
    return _con


@app.on_event("startup")
def startup():
    global _con
    if not DB_PATH.exists():
        log.error("explorer.duckdb not found at %s. Was build_data.py run during the image build?", DB_PATH)
        return
    _con = duckdb.connect(str(DB_PATH), read_only=True)
    counts = _con.execute(
        "SELECT (SELECT count(*) FROM funders), (SELECT count(*) FROM recipients), (SELECT count(*) FROM edges)"
    ).fetchone()
    log.info("Opened pre-built database: %s funders, %s recipients, %s edges", *counts)


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
