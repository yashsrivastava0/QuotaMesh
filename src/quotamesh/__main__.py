"""Allow ``python -m quotamesh`` when the console script is not on PATH."""

from quotamesh.cli import app

if __name__ == "__main__":
    app(prog_name="quotamesh")
