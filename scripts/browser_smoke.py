"""Isolated, provider-free browser journeys, layout checks, and failure injection."""

import argparse
import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import uvicorn
from playwright.sync_api import Error as BrowserError
from playwright.sync_api import expect, sync_playwright

from quotamesh.app import create_app
from quotamesh.demo import app as fake_app
from quotamesh.demo import configure_demo


def fake_provider_transport():
    """Answer every provider URL from the bundled fake upstream: no network, no real keys.

    Keys imported from a .env use the real registry endpoints, so listing and chat calls are
    mapped by path suffix onto the fake app in-process.
    """
    inner = httpx.ASGITransport(app=fake_app)
    dropped = {"host", "content-length", "transfer-encoding"}

    async def handler(request):
        path = request.url.path
        suffix = next((s for s in ("/models", "/chat/completions") if path.endswith(s)), path)
        forwarded = httpx.Request(
            request.method,
            "http://fake-provider/v1" + suffix,
            headers=[(k, v) for k, v in request.headers.items() if k.lower() not in dropped],
            content=await request.aread(),
        )
        response = await inner.handle_async_request(forwarded)
        body = await response.aread()
        headers = [(k, v) for k, v in response.headers.items() if k.lower() not in dropped]
        return httpx.Response(response.status_code, headers=headers, content=body)

    return httpx.MockTransport(handler)


@contextmanager
def local_server(demo, transport=None):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # A lock still held by a slow shutdown must not mask the real failure on Windows.
    with TemporaryDirectory(prefix="quotamesh-browser-", ignore_cleanup_errors=True) as directory:
        app = create_app(Path(directory), transport)
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
        # The page's own live-update line must leave "Connecting…" (no duplicate ids).
        expect(page.locator("#activity-feed-status")).to_have_text("Local updates connected.")
        assert page.locator("#activity-status").count() == 1
        # A failed listing check is reported as an error with a plain explanation.
        go(page, origin, "/access")
        page.locator("#credential-form").evaluate("form => form.closest('details').open = true")
        form.locator("[name=provider_id]").select_option("custom")
        form.locator("[name=plan_type]").select_option("FREE")
        form.locator("[name=secret_value]").fill("fake-401:rejected")
        form.locator("[name=base_url]").fill(origin + "/fake/v1")
        page.locator("#save-check").click()
        expect(page.locator("#notice")).to_contain_text("listing check failed")
        assert "success" not in (page.locator("#notice").get_attribute("class") or "")
        assert "rejected this API key" in page.locator("#notice").inner_text()
        page.close()


FAKE_ENV = """# QuotaMesh browser smoke: obviously fake values, never real keys
GEMINI_API_KEY=fake-200:smoke-gemini-not-a-real-key
export OPENAI_API_KEY="fake-200:smoke-openai-not-a-real-key"
GROQ_API_KEY=your_groq_key_here
UNRELATED_SETTING=hello
"""


def start_setup(context, screenshots=None):
    """Get started: paste a fake .env, import, list models, create qm/default, first reply."""
    with local_server(False, fake_provider_transport()) as (app, origin):
        page = context.new_page()
        page.goto(f"{origin}/bootstrap?token={app.state.bootstrap_token}")
        ready(page)
        go(page, origin, "/start")
        assert "prominent" in page.locator("nav a.nav-start").get_attribute("class")
        assert page.locator("nav a[aria-current=page]").inner_text() == "Get started"
        page.locator("#env-content").fill(FAKE_ENV)
        page.locator("#env-parse").click()
        expect(page.locator("#env-results")).to_be_visible()
        assert page.locator("#env-rows tr").count() == 4
        gemini = page.locator('#env-rows tr[data-name="GEMINI_API_KEY"]')
        assert gemini.locator("input").is_checked()
        assert gemini.locator("select").input_value() == "FREE"
        openai = page.locator('#env-rows tr[data-name="OPENAI_API_KEY"]')
        assert openai.locator("select").input_value() == "PAID"
        assert page.locator('#env-rows tr[data-name="GROQ_API_KEY"] input').is_disabled()
        assert "not-a-real-key" not in page.locator("#env-results").inner_text()
        page.locator("#env-import-button").click()
        expect(page.locator("#env-status")).to_contain_text("Imported 2 API key(s)")
        assert page.locator("#env-content").input_value() == "", "Pasted secrets are cleared"
        expect(page.locator('#step-key .step-state')).to_have_text("Done")
        page.locator("#check-keys").click()
        expect(page.locator("#check-status")).to_have_text("Checks finished.")
        assert page.locator(".key-check").filter(has_text="Found 2 model(s)").count() == 2
        page.locator(".key-check").filter(has_text="GEMINI_API_KEY").locator("select").select_option(
            "fake-free"
        )
        assert page.locator("[name=paid-mode][value=free]").is_checked()
        assert "GEMINI_API_KEY" in page.locator("#setup-key option:checked").inner_text()
        assert page.locator("#setup-model-id").input_value() == "fake-free"
        page.locator("#setup-default-form button[type=submit]").click()
        expect(page.locator("#notice")).to_contain_text("qm/default created")
        expect(page.locator('#step-profile .step-state')).to_have_text("Done")
        routing = app.state.store.safe_routing(datetime.now(UTC))
        assert [(t["provider_id"], t["model"]) for t in routing["targets"]] == [
            ("gemini", "fake-free")
        ]
        assert not routing["profile"]["allow_paid"]
        page.locator("#first-message-form button[type=submit]").click()
        expect(page.locator("#first-reply .first-reply")).to_have_text("Hello from fake upstream")
        expect(page.locator("#first-reply")).to_contain_text("Answered by gemini / fake-free")
        expect(page.locator('#step-message .step-state')).to_have_text("Done")
        assert not page.evaluate("localStorage.length || sessionStorage.length")
        if screenshots:
            page.screenshot(path=str(screenshots / "start-complete.png"), full_page=True)
        go(page, origin, "/start")
        assert "prominent" not in page.locator("nav a.nav-start").get_attribute("class")
        page.close()


def fresh_defaults(context):
    """Fresh-install defaults, profile picker names, and draft navigation regressions."""
    with local_server(False) as (app, origin):
        page = context.new_page()
        page.goto(f"{origin}/bootstrap?token={app.state.bootstrap_token}")
        ready(page)
        go(page, origin, "/connect")
        expect(page.locator("#integration-notice")).to_contain_text(
            "Save your default profile first"
        )
        assert "disabled" not in page.locator("#integration-notice").inner_text()
        go(page, origin, "/profiles")
        assert page.locator("[name=allow_trial]").is_checked(), "Trials default to allowed"
        assert page.locator("[name=nonstream_deadline_s]").input_value() == "90"
        assert page.locator("[name=first_event_timeout_s]").input_value() == "45"
        # Save a key without contacting the provider; new targets default to its provider.
        saved = page.request.post(
            f"{origin}/api/credentials",
            data={"provider_id": "groq", "plan_type": "FREE", "secret_value": "fake-untested"},
        )
        assert saved.status == 201, saved.text()
        go(page, origin, "/profiles")
        assert page.locator("[data-field=provider_id]").first.input_value() == "groq"
        page.locator("[data-field=model]").first.fill("llama-test")
        page.locator("#policy-form button[type=submit]").click()
        expect(page.locator("#notice")).to_contain_text("Profile saved")
        # Create, then rename: the picker always shows the saved name.
        page.locator("#new-profile").click()
        assert page.locator("[data-field=provider_id]").first.input_value() == "groq"
        page.locator("#profile-name").fill("Picker app")
        page.locator("#profile-slug").fill("picker-app")
        page.locator("[data-field=model]").first.fill("llama-test")
        page.locator("#policy-form button[type=submit]").click()
        page.wait_for_url("**/profiles?profile=picker-app")
        expect(page.locator("#profile-select option:checked")).to_have_text(
            "Picker app · qm/picker-app"
        )
        page.locator("#profile-name").fill("Picker renamed")
        page.locator("#policy-form button[type=submit]").click()
        expect(page.locator("#profile-select option:checked")).to_have_text(
            "Picker renamed · qm/picker-app"
        )
        # Escape on the leave dialog cancels navigation; a later save must not navigate.
        page.locator("#profile-name").fill("Picker escaped")
        page.locator("nav").get_by_role("link", name="Connect", exact=True).click()
        expect(page.locator("#leave-dialog")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#leave-dialog")).to_be_hidden()
        page.locator("#policy-form button[type=submit]").click()
        expect(page.locator("#profile-select option:checked")).to_have_text(
            "Picker escaped · qm/picker-app"
        )
        page.wait_for_timeout(300)
        assert "/profiles" in page.url, page.url
        page.close()


def playground(page, origin, screenshots=None):
    """Stream and non-stream Playground replies, metadata strip, and friendly errors."""
    bodies = []

    def capture(request):
        if request.method == "POST" and request.url.endswith("/api/test"):
            bodies.append(json.loads(request.post_data))

    page.on("request", capture)
    try:
        load(page, origin, "fallback")
        go(page, origin, "/playground")
        expect(page.locator("#playground-route")).to_contain_text("Next answer from")
        assert page.locator("#playground-stream").is_checked(), "Streaming is the default"
        assert page.locator("#playground-max-tokens").input_value() == "256"
        page.locator("#playground-message").fill("Hello there")
        page.locator("#playground-message").press("Control+Enter")
        first = page.locator(".message.assistant").nth(0)
        expect(first.locator(".meta-strip")).to_be_visible()
        expect(first.locator(".bubble")).to_have_text("Hello from fake upstream")
        strip = first.locator(".meta-strip").inner_text()
        for text in (
            "Provider custom",
            "Model fake-free",
            "Key Backup",
            "Attempts 2 · backup used",
            "Tokens 1 in · 4 out",
            "Finish stop",
            "First words",
        ):
            assert text in strip, (text, strip)
        href = first.locator(".meta-strip a").get_attribute("href")
        assert href.startswith("/activity?request=") and "profile=default" in href, href
        assert bodies[-1]["stream"] is True
        assert bodies[-1]["stream_options"] == {"include_usage": True}
        assert bodies[-1]["max_completion_tokens"] == 256
        # A second, non-stream turn sends the whole conversation, held only in the page.
        page.locator("#playground-stream").uncheck()
        page.locator("#playground-message").fill("And again")
        page.locator("#playground-send").click()
        second = page.locator(".message.assistant").nth(1)
        expect(second.locator(".meta-strip")).to_be_visible()
        expect(second.locator(".bubble")).to_have_text("Hello from fake upstream")
        strip = second.locator(".meta-strip").inner_text()
        assert "Tokens 1 in · 4 out" in strip and "Attempts 1 · no fallback" in strip, strip
        assert bodies[-1]["stream"] is False and "stream_options" not in bodies[-1]
        assert [m["role"] for m in bodies[-1]["messages"]] == ["user", "assistant", "user"]
        assert not page.evaluate("localStorage.length || sessionStorage.length")
        if screenshots:
            page.screenshot(path=str(screenshots / "playground-conversation.png"), full_page=True)
        page.locator("#playground-clear").click()
        assert not page.locator(".message").count()

        # Paid-only profile with paid off: a plain explanation and a link to fix it.
        created = page.request.post(
            f"{origin}/api/profiles",
            data={
                "slug": "paid-only",
                "name": "Paid only",
                "targets": [{"provider_id": "custom", "model": "demo-billable", "credential_id": 3}],
            },
        )
        assert created.status == 201, created.text()
        go(page, origin, "/playground", "paid-only")
        page.locator("#playground-message").fill("Hello")
        page.locator("#playground-send").click()
        card = page.locator(".message.assistant .error-card")
        expect(card).to_contain_text("paid use is off")
        assert "/profiles?profile=paid-only" in card.locator("a").first.get_attribute("href")
        assert page.locator("#playground-message").input_value() == "Hello", "Text restored"

        # Every target cooling down: candidates with reasons and a Retry-After countdown.
        load(page, origin, "paid-guard")
        go(page, origin, "/playground")
        page.locator("#playground-message").fill("Hello")
        page.locator("#playground-send").click()
        expect(page.locator(".message.assistant .error-card")).to_contain_text("rate-limited")
        page.locator("#playground-send").click()
        blocked = page.locator(".message.assistant .error-card").nth(1)
        expect(blocked).to_contain_text("can answer right now")
        assert blocked.locator("li").count() == 3
        expect(blocked.locator("[data-countdown]")).to_contain_text("retry")
        if screenshots:
            page.screenshot(path=str(screenshots / "playground-errors.png"), full_page=True)
    finally:
        page.remove_listener("request", capture)


def journeys(page, app, origin, screenshots, metrics):
    for path, name in [
        ("/", "overview"),
        ("/start", "start"),
        ("/access", "access"),
        ("/profiles", "profiles"),
        ("/playground", "playground"),
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
        # Status boxes never become grid items that shift a two-column layout.
        assert not page.locator(".grid > .panel-state, .catalog-grid > .panel-state").count()
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
    # "Use model in profile form" survives boot-time URL updates and adds a draft target.
    targets = len(app.state.store.safe_routing(datetime.now(UTC))["targets"])
    page.locator("#doctor-results").get_by_role("button", name="Use model in profile form").first.click()
    page.wait_for_url("**/profiles?profile=default*")
    ready(page)
    expect(page.locator("#notice")).to_contain_text("Model added to the draft")
    assert page.locator("#targets .target-row").count() == targets + 1
    assert page.locator("[data-field=model]").last.input_value() == "demo-billable"
    assert "model=" not in page.url, "Applied hints are removed from the address"
    page.locator(".brand").click()
    page.locator("#leave-discard").click()
    page.wait_for_url("**/?profile=default")
    go(page, origin, "/help/doctor")
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
        if scenario == "expired":
            # Recovery links lead to the page that fixes the block.
            link = page.locator("#decisions .blocked a.text-link").first
            assert "/access?profile=default&credential=1" in link.get_attribute("href")
        summary = send(page)
        if scenario == "fallback":
            page.locator("#refresh").click()
            ready(page)
            # The server's recovery_action.href wins over the dashboard's fallback mapping.
            explained = page.request.get(f"{origin}/api/explain?profile=default").json()
            action = next(d["recovery_action"] for d in explained["decisions"] if not d["eligible"])
            link = page.locator("#decisions .blocked a.text-link").first.get_attribute("href")
            assert link == (action.get("href") or link), (link, action)
            assert not link.startswith("/help?"), link
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

    playground(page, origin, screenshots)

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
    wallet_state = page.locator('.panel-state[data-for="#wallet"]')
    assert "Synthetic wallet outage" in wallet_state.inner_text()
    assert "failed" in wallet_state.get_attribute("class")
    assert "qm/default is saved" in page.locator("#journey-status").inner_text()
    page.unroute("**/api/wallet")
    wallet_state.get_by_role("button", name="Retry").click()
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
    assert (
        "Local request failed"
        in page.locator('.panel-state.failed[data-for="#decisions"]').inner_text()
    )
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
    # Nothing loaded, so there is nothing to copy: placeholder text is never copied.
    expect(page.locator("#copy-integration")).to_be_disabled()
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
            fresh_defaults(context)
            report["checks"].append(
                "fresh-install defaults, picker names after create/rename, Escape on leave dialog"
            )
            start_setup(context, screenshots)
            report["checks"].append(
                "Get started: fake .env parse/import, model listing, Free-only qm/default, "
                "first streamed reply (provider calls answered by the in-process fake)"
            )
            with local_server(True) as (app, origin):
                page = context.new_page()
                page.goto(f"{origin}/bootstrap?token={app.state.bootstrap_token}")
                ready(page)
                journeys(page, app, origin, screenshots, report["pages"])
                report["checks"].extend(
                    [
                        "eleven pages and responsive layout",
                        (
                            "Playground stream and non-stream replies with metadata strip, "
                            "multi-turn context, paid_blocked and no_candidates explanations"
                        ),
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
