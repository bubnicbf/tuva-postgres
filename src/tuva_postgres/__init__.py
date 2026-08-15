"""tuva_postgres: a reproducible, operable ingestion pipeline for Tuva-shaped
PostgreSQL snapshots.

Pipeline: HTTP snapshot manifest -> authenticated artifact downloads ->
immutable raw snapshot directory -> checksum/completeness validation ->
database migrations -> atomic snapshot load -> data-quality tests ->
operational run record and metrics.
"""
__version__ = "0.1.0"
