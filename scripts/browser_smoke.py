"""Isolated, provider-free browser journeys, layout checks, and failure injection."""

import argparse
import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from playwright.sync_api import Error as BrowserError
from playwright.sync_api import expect, sync_playwright

from quotamesh.app import create_app
from quotamesh.demo import app as fake_app
from quotamesh.demo import configure_demo


@contextmanager
def local_server(demo):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with TemporaryDirectory(prefix="quotamesh-browser-") as directory:
        app = create_app(Path(directory))
        origin = f"http://127.0.0.1:{port}"
        app.mount("/fake", fake_app)
        if demo:
            app.state.demo_mode = True
            app.state.demo_base_url = origin + "/fake/v1"
            configure_demo(app, app.state.demo_base_url, "wallet")
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="error")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            assert server.started, "Local test server did not start"
            yield app, origin
        finally:
            server.should_exit = True
            thread.join(timeout=10)


def ready(page):
    page.wait_for_function(
        "!document.querySelector('#page-load-status').textContent.includes('Loading') && !document.querySelector('[aria-busy=true]')"
    )


def go(page, origin, path, profile="default"):
    page.goto(f"{origin}{path}?profile={profile}")
    ready(page)


def send(page):
    page.locator("#test-form button[type=submit]").click()
    page.wait_for_function(
        "!document.querySelector('#test-form button[type=submit]').disabled && document.querySelector('#test-summary').textContent.includes('HTTP')"
    )
    return page.locator("#test-summary").inner_text()


def load(page, origin, scenario):
    go(page, origin, "/route")
    page.locator("#demo-form [name=scenario]").select_option(scenario)
    page.locator("#demo-form button").click()
    page.wait_for_url("**/route?profile=default&scenario=*")
    ready(page)
    assert page.locator("#demo-instructions").inner_text()


def onboarding(context):
    with local_server(False) as (app, origin):
        page = context.new_page()
        page.goto(f"{origin}/bootstrap?token={app.state.bootstrap_token}")
        ready(page)
        assert "Start with API access" in page.locator("#journey-status").inner_text()
        go(page, origin, "/access")
        form = page.locator("#credential-form")
        form.locator("[name=provider_id]").select_option("custom")
        form.locator("[name=plan_type]").select_option("FREE")
        form.locator("[name=secret_value]").fill("fake-200:onboarding")
        form.locator("[name=base_url]").fill(origin + "/fake/v1")
        page.locator("#save-check").click()
        page.wait_for_function(
            "document.querySelectorAll('#credential-list .credential').length === 1 && !document.querySelector('#save-check').disabled"
        )
        assert form.locator("[name=secret_value]").input_value() == ""
        assert not app.state.store.recent_attempts(), "Listing must not generate"
        page.locator("#setup-model").fill("fake-free")
        page.locator("#setup-form button").click()
        page.wait_for_url("**/route?profile=default")
        ready(page)
        assert "Next:" in page.locator("#explain-summary").inner_text()
        assert "HTTP 200" in send(page)
        go(page, origin, "/connect")
        assert origin + "/v1" in page.locator("#integration-code").inner_text()
        go(page, origin, "/activity")
        assert page.locator(".request-trace").count() == 1
        page.close()


def journeys(page, app, origin, screenshots, metrics):
    for path, name in [
        ("/", "overview"),
        ("/access", "access"),
        ("/profiles", "profiles"),
        ("/route", "route"),
        ("/connect", "connect"),
        ("/activity", "activity"),
        ("/help", "help"),
        ("/help/doctor", "doctor"),
        ("/help/catalog", "catalog"),
    ]:
        requests = []

        def record(request, requests=requests):
            if "/api/" in request.url and request.method == "GET":
                requests.append(request.url.split("/api/")[1].split("?")[0])

        page.on("request", record)
        start = time.perf_counter()
        go(page, origin, path)
        metrics[name] = {
            "get_requests": requests[:],
            "ready_ms": round((time.perf_counter() - start) * 1000),
        }
        page.remove_listener("request", record)
        assert len(requests) <= 2, (path, requests)
        assert page.locator("nav a[aria-current=page]").count() == 1
        assert not page.locator(".panel-state.failed").count(), path
        for width in (1440, 768, 390):
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                path,
                width,
            )
            if screenshots:
                page.screenshot(path=str(screenshots / f"{name}-{width}.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 900})
        # 200% desktop zoom is represented by half the CSS viewport dimensions.
        page.set_viewport_size({"width": 720, "height": 450})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), path
        page.set_viewport_size({"width": 1440, "height": 900})
        page.locator(".skip-link").focus()
        assert page.locator(".skip-link").evaluate("e => e.getBoundingClientRect().top >= 0")

    go(page, origin, "/profiles", "paid-backup")
    assert page.locator("#profile-slug").input_value() == "paid-backup"
    page.locator("#new-profile").click()
    assert page.locator("#targets .target-row").count() == 1
    assert not page.locator("[name=allow_paid]").is_checked()
    assert not page.locator("[name=allow_unknown_price]").is_checked()
    assert page.locator("[name=paid_daily_cap_usd]").input_value() == ""
    assert page.locator("[name=max_attempts]").input_value() == "5"
    page.locator("#profile-name").fill("Browser project")
    page.locator("#profile-slug").fill("browser-project")
    page.locator("[data-field=model]").fill("fake-free")
    page.locator("[data-field=credential_id]").select_option("2")
    page.locator("nav").get_by_role("link", name="Connect", exact=True).click()
    assert page.locator("#leave-dialog").is_visible()
    page.locator("#leave-stay").click()
    assert page.locator("#profile-name").input_value() == "Browser project"
    page.locator("nav").get_by_role("link", name="Connect", exact=True).click()
    page.locator("#leave-save").click()
    page.wait_for_url("**/connect?profile=browser-project")
    ready(page)
    go(page, origin, "/connect", "browser-project")
    assert "qm/browser-project" in page.locator("#integration-code").inner_text()
    for client in ("python", "node", "curl", "opencode", "env"):
        page.locator("#integration-client").select_option(client)
        assert page.locator("#integration-code").inner_text()
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
    page.locator("#copy-integration").click()
    expect(page.locator("#copy-status")).to_have_text("Copied.")
    snippet = page.locator("#integration-code").inner_text()
    copied = page.evaluate("navigator.clipboard.readText()")
    assert copied.replace("\r\n", "\n") == snippet.replace("\r\n", "\n"), repr(copied)
    # Exercise an asynchronous permission failure without relying on OS prompts.
    page.evaluate(
        """() => { window.originalClipboardWrite = navigator.clipboard.writeText;
        navigator.clipboard.writeText = () => new Promise((_, reject) => {
            setTimeout(() => reject(new DOMException('Denied', 'NotAllowedError')), 100);
        }); }"""
    )
    try:
        page.locator("#integration-client").select_option("python")
        page.locator("#copy-integration").click()
        expect(page.locator("#copy-status")).to_have_text(
            "Text selected. Press Ctrl+C or Command+C to copy."
        )
        assert page.evaluate("getSelection().toString()") == page.locator(
            "#integration-code"
        ).inner_text()
        expect(page.locator("#integration-code")).to_be_focused()
    finally:
        page.evaluate(
            "() => { navigator.clipboard.writeText = window.originalClipboardWrite; delete window.originalClipboardWrite; }"
        )
        page.context.clear_permissions()
    page.locator("#profile-select").select_option("free-app")
    page.wait_for_url("**/connect?profile=free-app")
    ready(page)
    page.go_back()
    ready(page)
    assert "qm/browser-project" in page.locator("#integration-alias").inner_text()
    go(page, origin, "/profiles", "browser-project")
    page.locator("#delete-profile").click()
    page.locator("#delete-profile").click()
    page.wait_for_function("document.querySelector('#profile-slug').value === 'default'")

    go(page, origin, "/help/doctor")
    count = len(app.state.store.recent_attempts())
    page.locator("#doctor-form button[type=submit]").click()
    page.wait_for_function(
        "document.querySelector('#doctor-status').textContent.includes('listing ok') && !document.querySelector('#doctor-form button[type=submit]').disabled"
    )
    assert len(app.state.store.recent_attempts()) == count
    page.locator("#doctor-mode").select_option("generation")
    page.locator("#doctor-credential").select_option("3")
    page.locator("#doctor-consent").check()
    page.locator("#doctor-form button[type=submit]").click()
    page.wait_for_function(
        "document.querySelector('#doctor-status').textContent.includes('paid blocked')"
    )
    assert len(app.state.store.recent_attempts()) == count

    for scenario, status, attempts in [
        ("fallback", 200, 2),
        ("paid-guard", 429, 2),
        ("paid-cap", 200, 3),
        ("shared-quota", 429, 1),
        ("error-first", 200, 2),
        ("midstream", 200, 1),
        ("success", 200, 1),
        ("expired", 200, 1),
        ("invalid", 200, 2),
        ("missing-env", 200, 1),
        ("unknown-cost", 200, 3),
        ("accounting", 429, 2),
    ]:
        load(page, origin, scenario)
        summary = send(page)
        assert f"HTTP {status}" in summary and f"{attempts} upstream attempt" in summary, (
            scenario,
            summary,
        )
        if scenario == "midstream":
            assert "Stream error" in page.locator("#test-output").inner_text()
        if scenario == "unknown-cost":
            assert (
                "HTTP 503" in send(page) or "HTTP 429" in page.locator("#test-summary").inner_text()
            )
        if scenario == "paid-cap":
            assert "HTTP 200" in send(page)
            assert "HTTP 429" in send(page)

    load(page, origin, "large")
    go(page, origin, "/access")
    assert page.locator("#credential-list .credential").count() == 38
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.set_viewport_size({"width": 1440, "height": 900})
    credential = page.locator("#credential-list .credential").last
    credential.get_by_role("button", name="Edit / rotate").click()
    page.locator("[name=label]").fill("Browser label <img src=x onerror=alert(1)>")
    page.locator("#credential-form button[type=submit]").click()
    page.wait_for_function(
        "document.querySelector('#credential-list').textContent.includes('Browser label <img')"
    )
    assert not page.locator("img").count()
    assert page.locator("[name=secret_value]").input_value() == ""
    credential.get_by_role("button", name="Delete", exact=True).click()
    credential.get_by_role("button", name="Confirm deletion").click()
    page.wait_for_function(
        "document.querySelectorAll('#credential-list .credential').length === 37"
    )
    page.locator("#preview-env").click()
    page.wait_for_function("document.querySelectorAll('#environment-results input').length === 5")
    assert "OPENAI_API_KEY" in page.locator("#environment-results").inner_text()
    assert not page.evaluate("localStorage.length || sessionStorage.length")

    # Simulate a failed wallet request: the route/setup panel must still render.
    load(page, origin, "wallet")
    page.route(
        "**/api/wallet",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"error":{"message":"Synthetic wallet outage"}}',
        ),
    )
    go(page, origin, "/")
    assert "Synthetic wallet outage" in page.locator("#wallet .panel-state.failed").inner_text()
    assert "qm/default is saved" in page.locator("#journey-status").inner_text()
    page.unroute("**/api/wallet")
    page.locator("#wallet").get_by_role("button", name="Retry").click()
    page.wait_for_function("document.querySelector('#wallet').dataset.stale === 'false'")
    assert not page.locator(".panel-state.failed:visible").count()
    go(page, origin, "/route")
    page.route(
        "**/api/explain*",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body="Malformed response"
        ),
    )
    page.locator("#refresh").click()
    page.wait_for_function("document.querySelector('#decisions').dataset.stale === 'true'")
    assert "Local request failed" in page.locator("#decisions .panel-state.failed").inner_text()
    assert "requests and" in page.locator("#spend-note").inner_text()
    page.unroute("**/api/explain*")

    page.goto(origin + "/#connect")
    page.wait_for_url("**/connect?profile=default")
    ready(page)
    for marker in ("fake-paid", "fake-200", '"secret_value":'):
        assert marker not in page.content()
    page.route(
        "**/api/integrations*",
        lambda route: route.fulfill(
            status=401,
            content_type="application/json",
            body='{"error":{"message":"Local authorization required"}}',
        ),
    )
    page.reload()
    ready(page)
    assert "session expired" in page.locator(".panel-state.failed").inner_text()
    page.unroute("**/api/integrations*")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-executable", default=os.environ.get("CHROMIUM_PATH"))
    parser.add_argument("--screenshots", default=os.environ.get("QM_SCREENSHOTS"))
    args = parser.parse_args()
    output = Path(args.screenshots) if args.screenshots else Path("output/frontend-browser")
    output.mkdir(parents=True, exist_ok=True)
    screenshots = output if args.screenshots else None
    report = {"pages": {}, "checks": []}
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(executable_path=args.browser_executable)
        except BrowserError as exc:
            raise SystemExit(
                "Browser could not start. Run `python -m playwright install chromium`, or pass --browser-executable with the Chrome/Edge executable path.\n"
                + str(exc).splitlines()[0]
            ) from None
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        errors = []
        context.on(
            "page", lambda page: page.on("pageerror", lambda error: errors.append(str(error)))
        )
        context.tracing.start(screenshots=True, snapshots=True)
        try:
            onboarding(context)
            report["checks"].append(
                "first-run setup, listing without generation, connection, and history"
            )
            with local_server(True) as (app, origin):
                page = context.new_page()
                page.goto(f"{origin}/bootstrap?token={app.state.bootstrap_token}")
                ready(page)
                journeys(page, app, origin, screenshots, report["pages"])
                report["checks"].extend(
                    [
                        "nine pages and responsive layout",
                        "profile CRUD, defaults, dirty Save/Stay, context and back navigation",
                        "clipboard write completion and delayed permission-denied fallback",
                        "credential edits, deletion, privacy and environment preview",
                        "Doctor listing and paid guard",
                        "12 routing and streaming scenarios",
                        "partial outage, retry, malformed response and session expiry",
                    ]
                )
                page.close()
            assert not errors, errors
            report["javascript_errors"] = errors
            report["result"] = "passed"
            context.tracing.stop()
        except Exception:
            context.tracing.stop(path=str(output / "failure-trace.zip"))
            if context.pages:
                context.pages[-1].screenshot(path=str(output / "failure.png"), full_page=True)
            report["result"] = "failed"
            raise
        finally:
            (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            context.close()
            browser.close()
    print(json.dumps(report))


if __name__ == "__main__":
    main()
