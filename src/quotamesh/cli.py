"""Minimal Phase 1 terminal entry points."""

from __future__ import annotations

import webbrowser

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from quotamesh.app import create_app
from quotamesh.config import data_directory
from quotamesh.store import Store

app = typer.Typer(no_args_is_help=True, help="Your local AI API capacity gateway")
console = Console()


@app.command()
def start(port: int = typer.Option(8787, min=1, max=65535), browser: bool = True) -> None:
    """Start the local dashboard and Chat Completions gateway."""
    server = create_app()
    url = f"http://127.0.0.1:{port}/bootstrap?token={server.state.bootstrap_token}"
    console.print("QuotaMesh is starting on 127.0.0.1")
    console.print("Open this one-time dashboard URL:")
    console.print(url, soft_wrap=True)
    if browser:
        webbrowser.open(url)
    uvicorn.run(server, host="127.0.0.1", port=port, workers=1, access_log=False)


@app.command()
def key() -> None:
    """Show the local gateway key for your own SDK configuration."""
    console.print(Store(data_directory()).gateway_key())


@app.command()
def status() -> None:
    """Show the configured Phase 1 connection without revealing its secret."""
    connection = Store(data_directory()).connection_summary()
    if not connection:
        console.print("No connection configured. Run `quotamesh start`.")
        return
    table = Table(title="QuotaMesh default connection")
    table.add_column("Provider")
    table.add_column("Label")
    table.add_column("Plan")
    table.add_column("Model")
    table.add_column("Paid enabled")
    table.add_row(
        connection["provider_id"],
        connection["label"],
        connection["plan_type"],
        connection["model"],
        "yes" if connection["allow_paid"] else "no",
    )
    console.print(table)


@app.command("fake-upstream")
def fake_upstream(port: int = typer.Option(8799, min=1, max=65535)) -> None:
    """Run a local fake provider for smoke tests; never uses real quota."""
    uvicorn.run("quotamesh.demo:app", host="127.0.0.1", port=port, access_log=False)
