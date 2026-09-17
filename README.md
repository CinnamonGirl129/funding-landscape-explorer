# Funding Landscape Explorer (v0.4 MVP)

An interactive network view of the U.S. nonprofit funding ecosystem, built on
[QWNTL Labs Open Grant Data](https://huggingface.co/datasets/qwntl-labs/open-grant-data),
the open-sourced successor to GrantX.

Search a nonprofit or a funder, then click any node to see its real,
documented funding neighborhood. Every dot and line traces to an actual
record; nothing is illustrative.

Built by Lauren Watson Grants LLC. Data is CC0 (public domain), courtesy of
QWNTL Labs and the GrantX lineage.

## What this is (and isn't) yet

This is a first real-data slice, not the full national landscape. It
supports: search, funder/recipient neighborhood expansion, a merged
(never-replaced) graph view, and a provenance panel on every node. It does
not yet include: time scrubbing, grant opportunities, semantic search, NST
scoring, or national-scale clustering. Those are deliberately staged for
later, once this core loop is proven out.

## Running it

This is a plain Docker web service (FastAPI + DuckDB). On start, it loads a
trimmed, indexed copy of the real QWNTL parquet files from Hugging Face into
a local database, then serves the API and the page. Deploy the repo as-is to
any Docker-capable host (Render, Fly.io, etc.); it reads the `$PORT`
environment variable that most hosts provide automatically.
