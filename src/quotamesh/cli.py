"""Small local gateway and credential-free demo commands."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

import httpx
import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from quotamesh import __version__
from quotamesh.app import create_app
from quotamesh.config import data_directory
from quotamesh.store import Store
from quotamesh.wallet import snapshot

app = typer.Typer(no_args_is_help=True, help="Your local AI API capacity gateway")
console = Console()
profile_app = typer.Typer(help="Inspect a saved project's routing policy")
app.add_typer(profile_app, name="profile")


@app.callback(invoke_without_command=True)
def version(version: bool = typer.Option(False, "--version", is_eager=True)):
    """Inspect the installed version without opening local storage."""
    if version:
        print(__version__)
        raise typer.Exit()


def run_server(server, port, browser):
    """Open a browser only once Uvicorn is ready to accept the bootstrap request."""
    runner = uvicorn.Server(
        uvicorn.Config(
            server,
            host="127.0.0.1",
            port=port,
            workers=1,
            access_log=False,
        )
    )
    if browser:

        def open_when_ready():
            for _ in range(300):
                if runner.started:
                    webbrowser.open(
                        f"http://127.0.0.1:{port}/bootstrap?token={server.state.bootstrap_token}"
                    )
                    return
                if runner.should_exit:
                    return
                time.sleep(0.1)

        threading.Thread(target=open_when_ready, daemon=True).start()
    runner.run()


def local_client(port):
    path = data_directory() / "gateway-key"
    if not path.exists():
        console.print("Start QuotaMesh first; no local gateway key exists.", markup=False)
        raise typer.Exit(1)
    return httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        trust_env=False,
        follow_redirects=False,
        headers={"Authorization": "Bearer " + path.read_text(encoding="utf-8").strip()},
        timeout=310,
    )


def local_api(path, *, port=8787, body=None, method="GET"):
    try:
        with local_client(port) as client:
            response = client.request(method, path, json=body)
            result = response.json()
            if not response.is_success:
                console.print(
                    result.get("error", {}).get("message", "Local request rejected"), markup=False
                )
                raise typer.Exit(1)
            return result
    except (httpx.HTTPError, ValueError):
        console.print(
            "Cannot reach the authorized local server. Check its port and data directory.",
            markup=False,
        )
        raise typer.Exit(1) from None


@app.command()
def start(port: int = typer.Option(8787, min=1, max=65535), browser: bool = True) -> None:
    """Start the local dashboard and Chat Completions gateway."""
    try:
        server = create_app()
    except (OSError, sqlite3.Error, RuntimeError):
        console.print(
            "Cannot open local storage. Check its permissions and supported schema version.",
            markup=False,
        )
        raise typer.Exit(1) from None
    url = f"http://127.0.0.1:{port}/bootstrap?token={server.state.bootstrap_token}"
    console.print("QuotaMesh is starting on 127.0.0.1")
    console.print("Open this one-time dashboard URL:")
    console.print(url, soft_wrap=True)
    run_server(server, port, browser)


@app.command()
def key(data_dir: Path | None = None) -> None:
    """Show the local gateway key for your own SDK configuration."""
    print(Store(data_dir or data_directory()).gateway_key())


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
        console.print(f'Demo client key: quotamesh key --data-dir "{directory}"', markup=False)
        console.print(url, soft_wrap=True)
        run_server(server, port, browser)


@app.command()
def doctor(
    credential: int = typer.Argument(..., min=1),
    generation: bool = False,
    consent: bool = False,
    profile: str = "default",
    position: int | None = typer.Option(None, min=1, max=30),
    port: int = typer.Option(8787, min=1, max=65535),
):
    """Explicit listing check; generation needs --generation --consent and --position."""
    result = local_api(
        "/api/doctor",
        port=port,
        method="POST",
        body={
            "credential_id": credential,
            "mode": "generation" if generation else "models",
            "consent": consent,
            "profile": profile,
            "position": position,
        },
    )
    console.print(json.dumps(result, indent=2), markup=False)
    if result["outcome"] not in {"listing_ok", "ok"}:
        raise typer.Exit(1)


@profile_app.command("test")
def profile_test(slug: str, port: int = typer.Option(8787, min=1, max=65535)):
    """Dry run saved candidates; makes no provider call."""
    result = local_api("/api/explain?profile=" + slug, port=port)
    table = Table(title="Saved policy: qm/" + slug)
    for column in ("Order", "Credential", "Model", "Decision", "Recovery"):
        table.add_column(column)
    for d in result["decisions"]:
        table.add_row(
            str(d["position"]),
            d["label"],
            d["model"],
            "eligible" if d["eligible"] else d["skip_reason"],
            d["recovery_at"] or "unknown",
        )
    console.print(table)
    console.print(result["notice"], markup=False)


@app.command("env")
def environment(
    slug: Annotated[str, typer.Argument()] = "default",
    shell: str = "powershell" if os.name == "nt" else "bash",
    port: int = typer.Option(8787, min=1, max=65535),
):
    """Print environment instructions for a saved profile; no provider keys."""
    result = local_api(f"/api/integrations?profile={slug}&shell={shell}", port=port)
    print(result["snippets"]["env"])


@app.command("import-env")
def import_environment(
    names: Annotated[list[str] | None, typer.Argument()] = None,
    port: int = typer.Option(8787, min=1, max=65535),
):
    """Import present server-process keys as environment references and UNKNOWN plans."""
    preview = local_api("/api/environment", port=port)
    present = [v["env_name"] for v in preview["variables"] if v["present"] and not v["configured"]]
    if not names:
        names = present
    if not names:
        console.print("No new known variables in the server environment. Restart it with keys set.")
        return
    result = local_api("/api/environment/import", port=port, method="POST", body={"names": names})
    console.print(json.dumps(result, indent=2), markup=False)
    console.print("Imported as UNKNOWN. No targets or paid permissions were changed.")


@app.command()
def add(
    provider: str = typer.Option(...),
    plan: str = "UNKNOWN",
    env_name: str | None = None,
    label: str | None = None,
    base_url: str | None = None,
    port: int = typer.Option(8787, min=1, max=65535),
):
    """Save access as untested. Prefer --env-name, or enter a hidden secret interactively."""
    body = {"provider_id": provider, "plan_type": plan, "label": label}
    if base_url:
        body["base_url"] = base_url
    if env_name:
        body["env_name"] = env_name
    else:
        body["secret_value"] = typer.prompt("Provider API key", hide_input=True)
    result = local_api("/api/credentials", port=port, method="POST", body=body)
    console.print(
        f"Saved credential {result['credential_id']} as untested. Configure a profile target."
    )


@app.command()
def reset(
    credential: int = typer.Argument(..., min=1), port: int = typer.Option(8787, min=1, max=65535)
):
    """Clear shared health/quota state; never clears spend or restores provider quota."""
    local_api(
        f"/api/credentials/{credential}/action", port=port, method="POST", body={"action": "reset"}
    )
    console.print("State reset. Provider quota and observed spending are unchanged.")


@app.command()
def tail(port: int = typer.Option(8787, min=1, max=65535)):
    """Follow local metadata activity; Ctrl+C stops the feed."""
    try:
        with local_client(port) as client, client.stream("GET", "/events") as response:
            if response.status_code != 200:
                console.print("Local activity authorization failed.")
                raise typer.Exit(1)
            for line in response.iter_lines():
                if line.startswith("data:"):
                    console.print(line[5:].strip(), markup=False)
    except httpx.HTTPError:
        console.print("Local activity disconnected. Check the server port.")
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        return
