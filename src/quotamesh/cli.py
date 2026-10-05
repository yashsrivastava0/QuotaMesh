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
from quotamesh.wallet import snapshot

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
    """Show grouped capacity, observation sources, and each named project policy."""
    store = Store(data_directory())
    now = datetime.now(UTC)
    wallet = snapshot(store, now)
    table = Table(title="QuotaMesh capacity — local observations only")
    for title in (
        "Plan [MANUAL]",
        "Source [MANUAL]",
        "Keys [LOCAL]",
        "Requests / attempts today [LOCAL]",
        "Last success [LOCAL]",
    ):
        table.add_column(title)
    for plan, groups in wallet["buckets"].items():
        for group in groups:
            table.add_row(
                plan,
                group["group"],
                str(len(group["keys"])),
                f"{group['today']['routed_requests']} / {group['today']['attempts']}",
                group["usage"]["last_success_at"] or "not observed",
            )
    console.print(table)
    if not any(wallet["buckets"].values()):
        console.print("No credentials configured. Run `quotamesh start` or `quotamesh demo`.")
    policies = Table(title="Project profiles — UTC paid caps")
    for title in (
        "Alias",
        "Name",
        "Enabled",
        "Targets",
        "Trial / paid",
        "Daily / monthly cap",
        "Paid today [LOCAL]",
    ):
        policies.add_column(title)
    for profile in wallet["profiles"]:
        routing = store.safe_routing(now, profile["slug"])
        caps = " / ".join(
            "none" if profile[field] is None else f"${profile[field]:.4f}"
            for field in ("paid_daily_cap_usd", "paid_monthly_cap_usd")
        )
        policies.add_row(
            "qm/" + profile["slug"],
            profile["name"],
            "yes" if profile["enabled"] else "no",
            str(len(routing["targets"])),
            f"{'on' if profile['allow_trial'] else 'off'} / {'on' if profile['allow_paid'] else 'off'}",
            caps,
            f"${routing['usage']['daily']:.4f}"
            + (" + unknown" if routing["usage"]["unknown_daily"] else ""),
        )
    console.print(policies)
    console.print(wallet["limitations"], markup=False)
    console.print("Usage coverage: " + wallet["coverage"], markup=False)


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
        configure_demo(server, server.state.demo_base_url, "wallet")
        server.mount("/demo-upstream", fake_app)
        url = f"http://127.0.0.1:{port}/bootstrap?token={server.state.bootstrap_token}"
        console.print("QuotaMesh demo: temporary data, simulated dollars, no provider keys.")
        console.print(url, soft_wrap=True)
        if browser:
            webbrowser.open(url)
        uvicorn.run(server, host="127.0.0.1", port=port, workers=1, access_log=False)
