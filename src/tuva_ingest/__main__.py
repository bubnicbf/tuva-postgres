"""`python -m tuva_ingest` -- identical to the `tuva-ingest` console script
entry point (see pyproject.toml [project.scripts])."""
from .cli import main

if __name__ == "__main__":
    import sys

    sys.exit(main())
