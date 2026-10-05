"""Small local gateway and credential-free demo commands."""

from __future__ import annotations

import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

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
    """Show safe credential and default-policy summaries."""
    routing = Store(data_directory()).safe_routing(datetime.now(UTC))
    if not routing["credentials"]:
        console.print("No credentials configured. Run `quotamesh start` or `quotamesh demo`.")
        return
    table = Table(title="QuotaMesh API access — local metadata only")
    for title in ("Provider", "Label", "Plan", "State", "Quota group", "Expiry"):
        table.add_column(title)
    for credential in routing["credentials"]:
        table.add_row(
            credential["provider_id"],
            credential["label"],
            credential["plan_type"],
            credential["status"] if credential["enabled"] else "DISABLED",
            credential["quota_group"] or "provider default",
            (credential["trial_expires_at"] or "not set")[:10],
        )
    console.print(table)
    profile = routing["profile"]
    if profile:
        console.print(
            f"qm/default: {len(routing['targets'])} ordered target(s), "
            f"paid {'on' if profile['allow_paid'] else 'off'}, "
            f"maximum {profile['max_attempts']} attempts"
        )
        console.print(
            f"Observed paid spend today: ${routing['usage']['daily']:.4f} (UTC). "
            "Only traffic through QuotaMesh is counted."
        )


@app.command("fake-upstream")
def fake_upstream(port: int = typer.Option(8799, min=1, max=65535)) -> None:
    """Run a local fake provider for smoke tests; never uses real quota."""
    uvicorn.run("quotamesh.demo:app", host="127.0.0.1", port=port, access_log=False)


@app.command()
def demo(port: int = typer.Option(8788, min=1, max=65535), browser: bool = True) -> None:
    """Explore fallback and paid safety with isolated fake credentials, never real quota."""
    from quotamesh.demo import app as fake_app
    from quotamesh.demo import configure_demo

    with TemporaryDirectory(prefix="quotamesh-demo-") as directory:
        server = create_app(Path(directory))
        server.state.demo_mode = True
        server.state.demo_base_url = f"http://127.0.0.1:{port}/demo-upstream/v1"
        configure_demo(server, server.state.demo_base_url)
        server.mount("/demo-upstream", fake_app)
        url = f"http://127.0.0.1:{port}/bootstrap?token={server.state.bootstrap_token}"
        console.print("QuotaMesh demo: temporary data, simulated dollars, no provider keys.")
        console.print(url, soft_wrap=True)
        if browser:
            webbrowser.open(url)
        uvicorn.run(server, host="127.0.0.1", port=port, workers=1, access_log=False)
