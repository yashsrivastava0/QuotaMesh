"""Small local gateway and credential-free demo commands."""

from __future__ import annotations

import json
import os
import socket
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
from quotamesh.envfile import EnvFileError, is_placeholder, parse_env, provider_for
from quotamesh.lifecycle import process_lock
from quotamesh.security import mint_bootstrap_token
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
                    # A separate expiring token keeps the printed link usable elsewhere.
                    token = mint_bootstrap_token(server)
                    webbrowser.open(f"http://127.0.0.1:{port}/bootstrap?token={token}")
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


def port_available(port: int) -> bool:
    """True when 127.0.0.1:port can be bound right now."""
    with socket.socket() as probe:
        if os.name != "nt":
            # Like uvicorn: a port in TIME_WAIT after a graceful restart is still free.
            # (On Windows SO_REUSEADDR would allow binding a port that is in use.)
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def load_env_file(path: Path) -> None:
    """Expose provider key variables from a .env file to this server process.

    Only recognised provider names are loaded, existing variables win, and values are
    never printed.
    """
    try:
        parsed = parse_env(path.read_bytes())
    except (OSError, EnvFileError) as exc:
        message = exc.strerror if isinstance(exc, OSError) else str(exc)
        console.print(f"Cannot read {path}: {message}", markup=False)
        raise typer.Exit(1) from None
    loaded = []
    for name, entry in parsed.entries.items():
        value = entry.value.reveal()
        if provider_for(name) and not is_placeholder(value) and name not in os.environ:
            os.environ[name] = value
            loaded.append(name)
    console.print(
        "Loaded from " + str(path) + ": " + (", ".join(loaded) or "no new provider keys"),
        markup=False,
    )


@app.command()
def start(
    port: int = typer.Option(8787, min=1, max=65535),
    browser: bool = True,
    env_file: Annotated[
        Path | None,
        typer.Option(
            "--env-file",
            help="Load provider key variables from this .env file for environment references",
        ),
    ] = None,
) -> None:
    """Start the local dashboard and Chat Completions gateway."""
    if env_file is not None:
        load_env_file(env_file)
    if not port_available(port):
        hint = "quotamesh open" if port == 8787 else f"quotamesh open --port {port}"
        console.print(
            f"Port {port} is busy. If QuotaMesh is already running, use `{hint}`; "
            "otherwise choose another --port.",
            markup=False,
        )
        raise typer.Exit(1)
    try:
        with process_lock(data_directory()):
            pass
    except (RuntimeError, OSError):
        console.print(
            "QuotaMesh is already running with this data directory. "
            "Run `quotamesh open` to get a fresh dashboard link.",
            markup=False,
        )
        raise typer.Exit(1) from None
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


@app.command("open")
def open_dashboard(
    port: int = typer.Option(8787, min=1, max=65535),
    print_url: Annotated[
        bool, typer.Option("--print", help="Print the one-time link instead of opening it")
    ] = False,
) -> None:
    """Open the running dashboard with a fresh one-time login link (valid for 2 minutes)."""
    try:
        with local_client(port) as client:
            response = client.post("/api/session/link")
    except httpx.HTTPError:
        console.print(
            f"QuotaMesh is not running on port {port}. Start it with `quotamesh start`.",
            markup=False,
        )
        raise typer.Exit(1) from None
    if response.status_code != 201:
        console.print(
            f"The server on port {port} did not accept this data directory's local key. "
            "Check --port and QUOTAMESH_DATA_DIR.",
            markup=False,
        )
        raise typer.Exit(1)
    url = response.json()["url"]
    if print_url:
        print(url)
    elif webbrowser.open(url):
        console.print("Opened the QuotaMesh dashboard in your browser.")
    else:
        console.print("Could not open a browser. Use this one-time link within 2 minutes:")
        console.print(url, soft_wrap=True, markup=False)


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


PLAN_CHOICES = ("FREE", "TRIAL_CREDIT", "PAID", "UNKNOWN")


def plan_overrides(values):
    overrides = {}
    for value in values or []:
        name, separator, plan = value.partition("=")
        plan = plan.strip().upper()
        if not separator or not name.strip() or plan not in PLAN_CHOICES + ("SKIP",):
            console.print(
                "Use --plan NAME=PLAN with FREE, TRIAL_CREDIT, PAID, UNKNOWN or SKIP.",
                markup=False,
            )
            raise typer.Exit(2)
        overrides[name.strip()] = plan
    return overrides


def read_env_file(path):
    """Read .env text locally; only its content (never the path) goes to the server."""
    from quotamesh.envfile import MAX_BYTES

    try:
        if path.stat().st_size > MAX_BYTES:
            console.print("The .env file is larger than 64 KiB.", markup=False)
            raise typer.Exit(1)
        return path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        console.print("The .env file is not UTF-8 text.", markup=False)
        raise typer.Exit(1) from None
    except OSError:
        console.print("Cannot read that .env file. Check its path and permissions.", markup=False)
        raise typer.Exit(1) from None


def import_env_file(path, plans, store, yes, port):
    """Preview a .env file through the local server, choose plans, then import."""
    overrides = plan_overrides(plans)
    content = read_env_file(path)
    preview = local_api("/api/environment/parse", port=port, method="POST", body={"content": content})
    table = Table(title="Provider keys in " + path.name + " (values are never shown)")
    for column in ("Name", "Provider", "Fingerprint", "Status", "Suggested plan"):
        table.add_column(column)
    for entry in preview["entries"]:
        table.add_row(
            entry["name"],
            entry["provider_id"] or "-",
            entry["fingerprint"] or "-",
            entry["status"],
            entry["suggested_plan"] or "-",
        )
    console.print(table)
    for warning in preview["warnings"]:
        console.print(f"Line {warning['line']}: {warning['message']}", markup=False)
    candidates = [entry for entry in preview["entries"] if entry["status"] == "new"]
    for name in sorted(set(overrides) - {entry["name"] for entry in candidates}):
        console.print(f"--plan {name}: no new key with that name; ignored.", markup=False)
    items = []
    for entry in candidates:
        plan = overrides.get(entry["name"])
        if plan is None and yes:
            plan = entry["suggested_plan"]
        while plan not in PLAN_CHOICES + ("SKIP",):
            plan = (
                typer.prompt(
                    f"Plan for {entry['name']} ({entry['provider_id']}): "
                    "FREE, TRIAL_CREDIT, PAID, UNKNOWN or SKIP",
                    default=entry["suggested_plan"],
                )
                .strip()
                .upper()
            )
        if plan != "SKIP":
            items.append({"name": entry["name"], "plan_type": plan})
    if not items:
        console.print("No new provider keys to import.")
        return
    result = local_api(
        "/api/environment/import-file",
        port=port,
        method="POST",
        body={"content": content, "store": store, "items": items},
    )
    for row in result["imported"]:
        console.print(
            f"Imported {row['name']} as credential {row['credential_id']} "
            f"({row['provider_id']}, {row['plan_type']}).",
            markup=False,
        )
    for row in result["skipped"]:
        console.print(f"Skipped {row['name']}: {row['reason']}.", markup=False)
    for row in result["warnings"]:
        console.print(f"{row['name']}: {row['message']}", markup=False)
    if result["imported"]:
        console.print(
            "Next: run `quotamesh doctor <credential>` to check listing access, then add the "
            "keys to a profile in the dashboard (`quotamesh open`). Paid and UNKNOWN plans stay "
            "blocked until a profile explicitly allows paid use.",
            markup=False,
        )


@app.command("import-env")
def import_environment(
    names: Annotated[list[str] | None, typer.Argument()] = None,
    file: Annotated[
        Path | None, typer.Option("--file", help="Read provider keys from this .env file")
    ] = None,
    plan: Annotated[
        list[str] | None, typer.Option("--plan", help="NAME=PLAN; repeat for several keys")
    ] = None,
    store: Annotated[
        str,
        typer.Option(
            "--store", help="value: save the keys; reference: save variable names only"
        ),
    ] = "value",
    yes: Annotated[bool, typer.Option("--yes", help="Accept suggested plans")] = False,
    port: int = typer.Option(8787, min=1, max=65535),
):
    """Import keys from a .env file (--file), or server-process variables as references."""
    if file is not None:
        if names:
            console.print("Use either --file or variable names, not both.", markup=False)
            raise typer.Exit(2)
        if store not in {"value", "reference"}:
            console.print("Use --store value or --store reference.", markup=False)
            raise typer.Exit(2)
        import_env_file(file, plan, store, yes, port)
        return
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
