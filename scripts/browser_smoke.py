"""Optional real-browser smoke: pip install playwright; use system Chromium."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1]
process = subprocess.Popen(
    [str(root / ".venv/bin/quotamesh"), "demo", "--no-browser", "--port", "8808"],
    cwd=root,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
)
try:
    url = None
    for _ in range(20):
        line = process.stdout.readline()
        match = re.search(r"http://127\.0\.0\.1:8808/bootstrap\?token=\S+", line)
        if match:
            url = match.group(0)
        if "Uvicorn running" in line:
            break
    assert url, "Demo did not publish a bootstrap URL"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium")
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url)
        page.wait_for_selector(".credential")
        assert page.locator(".credential").count() == 3
        assert "fake-429-short" not in page.content()
        assert page.locator(".decision-badge").first.inner_text() == "NEXT"

        def scenario(name):
            page.locator("#demo-form select").select_option(name)
            with page.expect_response("**/api/demo") as response:
                page.locator("#demo-form button").click()
            assert response.value.status == 200
            page.wait_for_function(
                "document.querySelector('#notice').textContent.startsWith('Demo reset')"
            )

        def send(stream=False):
            page.locator("#test-form input[name=stream]").set_checked(stream)
            with page.expect_response("**/api/test") as response:
                page.locator("#test-form button[type=submit]").click()
            page.wait_for_function(
                "document.querySelector('#test-form button[type=submit]').disabled === false"
            )
            return response.value

        first = send()
        assert first.status == 200 and first.headers["x-quotamesh-attempts"] == "2"
        assert "Hello from fake upstream" in page.locator("#test-output").inner_text()
        assert page.locator("#attempts tr").count() == 2
        # Saving the UI's policy fields must round-trip without switching price/permission policy.
        with page.expect_response("**/api/policy") as response:
            page.locator("#policy-form button[type=submit]").click()
        assert response.value.status == 200
        scenario("paid-guard")
        assert send().status == 429
        assert "Paid use is off" in page.locator("#decisions").inner_text()
        scenario("paid-cap")
        assert send().status == 200
        assert send().status == 200
        assert send().status == 429
        assert "Observed paid cap reached" in page.locator("#decisions").inner_text()
        scenario("shared-quota")
        assert send().status == 429
        assert page.locator("#attempts tr").count() == 1
        assert page.locator(".decision.blocked").count() == 3
        scenario("error-first")
        result = send(True)
        assert result.status == 200 and result.headers["x-quotamesh-attempts"] == "2"
        assert "Hello from fake upstream" in page.locator("#test-output").inner_text()
        scenario("midstream")
        result = send(True)
        assert result.status == 200 and result.headers["x-quotamesh-attempts"] == "1"
        assert "Stream error:" in page.locator("#test-output").inner_text()
        assert "MID_STREAM_FAILURE" in page.locator("#attempts").inner_text()
        scenario("fallback")
        page.screenshot(path="/tmp/quotamesh-phase-two-desktop.png", full_page=True)
        # Verify adding a new credential and target works through the actual forms.
        page.locator("#credential-form").locator("xpath=..").evaluate(
            "(details) => details.open = true"
        )
        page.locator("#credential-form [name=provider_id]").select_option("custom")
        page.locator("#credential-form [name=label]").fill("Browser test key")
        page.locator("#credential-form [name=secret_value]").fill("fake-200")
        page.locator("#credential-form [name=base_url]").fill(
            "http://127.0.0.1:8808/demo-upstream/v1"
        )
        with page.expect_response("**/api/credentials") as response:
            page.locator("#credential-form button").click()
        assert response.value.status == 201
        page.wait_for_function("document.querySelectorAll('.credential').length === 4")
        page.locator("#add-target").click()
        page.locator(".target-row").last.locator("[data-field=model]").fill("fake-browser")
        page.locator(".target-row").last.locator("[data-field=credential_id]").select_option("4")
        with page.expect_response("**/api/policy") as response:
            page.locator("#policy-form button[type=submit]").click()
        assert response.value.status == 200
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path="/tmp/quotamesh-phase-two-mobile.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
            "Mobile page overflows"
        )
        assert errors == [], errors
        browser.close()
    print(
        "Browser smoke passed: six demo scenarios, policy save, credential/target forms, streaming output, and mobile layout."
    )
finally:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
