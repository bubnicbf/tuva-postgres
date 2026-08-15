"""`python -m tuva_postgres` -- identical to the `tuva-postgres` console
script entry point (see pyproject.toml [project.scripts])."""
from .cli import main

if __name__ == "__main__":
    import sys

    sys.exit(main())
