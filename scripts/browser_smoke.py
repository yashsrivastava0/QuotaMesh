"""Optional real-browser acceptance. Run with playwright installed and Chromium available."""

import json
import os
import socket
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from playwright.sync_api import sync_playwright

from quotamesh.app import create_app
from quotamesh.demo import app as fake_app
from quotamesh.demo import configure_demo


def main():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with TemporaryDirectory(prefix="quotamesh-browser-") as directory:
        app = create_app(Path(directory))
        app.state.demo_mode = True
        app.state.demo_base_url = f"http://127.0.0.1:{port}/demo-upstream/v1"
        configure_demo(app, app.state.demo_base_url, "wallet")
        app.mount("/demo-upstream", fake_app)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="error")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        try:
            with sync_playwright() as playwright:
                executable = os.environ.get("CHROMIUM_PATH")
                browser = playwright.chromium.launch(
                    executable_path=executable, args=["--no-sandbox"]
                )
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}/bootstrap?token={app.state.bootstrap_token}")
                page.wait_for_function(
                    "document.querySelectorAll('#credential-list .credential').length === 5"
                )
                assert page.locator("#credential-list .credential").count() == 5
                assert page.locator(".wallet-bucket").count() == 3
                assert page.locator(".trial_credit .capacity-source").count() == 1
                assert "2 key(s)" in page.locator(".trial_credit").inner_text()
                assert "Not reported [UNKNOWN]" in page.locator(".trial_credit").inner_text()
                assert not any(
                    secret in page.content() for secret in ("fake-429-short", '"secret_value":')
                )
                page.locator("#profile-select").select_option("paid-backup")
                page.wait_for_function(
                    "document.querySelector('#connection-alias').textContent === 'qm/paid-backup'"
                )
                page.locator("#test-form [name=stream]").check()
                page.locator("#test-form button[type=submit]").click()
                page.wait_for_function(
                    "document.querySelector('#test-summary').textContent.includes('HTTP 200') && !document.querySelector('#test-form button[type=submit]').disabled"
                )
                assert "Hello from fake upstream" in page.locator("#test-output").inner_text()
                assert "$19.4000" in page.locator(".trial_credit").inner_text()
                page.locator("#request-history .request-trace").first.locator("summary").click()
                assert "Hackathon trial" in page.locator("#request-history").inner_text()
                assert "[PROVIDER]" in page.locator("#request-history").inner_text()
                page.locator("#history-filter").select_option("selected")
                page.locator("#new-profile").click()
                page.locator("#profile-name").fill("Browser app")
                page.locator("#profile-slug").fill("browser-app")
                page.locator("#profile-template").select_option("free-trial")
                page.locator("#policy-form button[type=submit]").click()
                page.wait_for_function(
                    "document.querySelector('#connection-alias').textContent === 'qm/browser-app'"
                )
                assert page.locator("#profile-slug").get_attribute("readonly") is not None
                assert not page.locator("#policy-form [name=allow_paid]").is_checked()
                # Edit label, preserving the write-only key; then rotate to another fake secret.
                card = page.locator("#credential-list .credential").nth(3)
                card.get_by_role("button", name="Edit / rotate").click()
                assert page.locator("#credential-form [name=secret_value]").input_value() == ""
                page.locator("#credential-form [name=label]").fill("Rotated trial")
                page.locator("#credential-form button[type=submit]").click()
                page.wait_for_function(
                    "document.querySelector('#credential-list').textContent.includes('Rotated trial')"
                )
                page.locator("#credential-list .credential").nth(3).get_by_role(
                    "button", name="Edit / rotate"
                ).click()
                page.locator("#credential-form [name=secret_value]").fill("fake-200")
                page.locator("#credential-form button[type=submit]").click()
                page.wait_for_function("document.querySelector('#cancel-edit').hidden")
                page.locator("#test-form button[type=submit]").click()
                page.wait_for_function(
                    "document.querySelector('#test-summary').textContent.includes('HTTP 200') && !document.querySelector('#test-form button[type=submit]').disabled"
                )
                assert "Hello from fake upstream" in page.locator("#test-output").inner_text()
                assert (
                    "Locally estimated USD: $0.0000 [LOCAL]"
                    in page.locator(".trial_credit").inner_text()
                )  # Provider cost is not invented; token-based estimation is local.
                # Scope-specific history refresh and disabling the profile.
                page.locator("#policy-form [name=enabled]").uncheck()
                page.locator("#policy-form button[type=submit]").click()
                page.wait_for_function(
                    "[...document.querySelectorAll('.decision-badge')].every(e => e.textContent === 'SKIP')"
                )
                page.locator("#test-form button[type=submit]").click()
                page.wait_for_function(
                    "document.querySelector('#test-summary').textContent.includes('HTTP 503') && !document.querySelector('#test-form button[type=submit]').disabled"
                )
                page.locator("#delete-profile").click()
                page.locator("#delete-profile").click()
                page.wait_for_function(
                    "document.querySelector('#connection-alias').textContent === 'qm/default'"
                )
                # Exercise every Phase 2 failure demo through the UI.
                for scenario, streamed, status, attempts in [
                    ("fallback", False, 200, 2),
                    ("paid-guard", False, 429, 2),
                    ("paid-cap", False, 200, 3),
                    ("shared-quota", False, 429, 1),
                    ("error-first", True, 200, 2),
                    ("midstream", True, 200, 1),
                ]:
                    page.locator("#demo-form [name=scenario]").select_option(scenario)
                    page.locator("#demo-form button").click()
                    page.wait_for_function(
                        "document.querySelector('#test-summary').textContent === 'No test sent in this scenario yet.'"
                    )
                    page.locator("#test-form [name=stream]").set_checked(streamed)
                    page.locator("#test-form button[type=submit]").click()
                    page.wait_for_function(
                        "!document.querySelector('#test-form button[type=submit]').disabled"
                    )
                    summary = page.locator("#test-summary").inner_text()
                    assert (
                        f"HTTP {status}" in summary and f"{attempts} upstream attempt" in summary
                    ), (scenario, summary)
                    if scenario == "midstream":
                        assert "Stream error" in page.locator("#test-output").inner_text()
                # Add access without a model/label, edit text safely, and delete an unpinned key.
                page.locator("#credential-form [name=provider_id]").select_option("custom")
                page.locator("#credential-form [name=plan_type]").select_option("TRIAL_CREDIT")
                page.locator("#credential-form [name=secret_value]").fill("fake-200")
                page.locator("#credential-form [name=base_url]").fill(app.state.demo_base_url)
                page.locator("#credential-form button[type=submit]").click()
                page.wait_for_function(
                    "document.querySelectorAll('#credential-list .credential').length === 4"
                )
                page.locator("#credential-list .credential").last.get_by_role(
                    "button", name="Edit / rotate"
                ).click()
                page.locator("#credential-form [name=label]").fill(
                    "Browser access <img src=x onerror=alert(1)>"
                )
                page.locator("#credential-form button[type=submit]").click()
                page.wait_for_function(
                    "document.querySelector('#credential-list').textContent.includes('Browser access <img')"
                )
                assert page.locator("img").count() == 0
                last = page.locator("#credential-list .credential").last
                last.get_by_role("button", name="Delete", exact=True).click()
                last.get_by_role("button", name="Confirm deletion").click()
                page.wait_for_function(
                    "document.querySelectorAll('#credential-list .credential').length === 3"
                )
                # Mobile layout remains within viewport; save optional review screenshots.
                page.locator("#demo-form [name=scenario]").select_option("wallet")
                page.locator("#demo-form button").click()
                page.wait_for_function(
                    "document.querySelectorAll('#credential-list .credential').length === 5"
                )
                if output := os.environ.get("QM_SCREENSHOTS"):
                    Path(output).mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(Path(output) / "wallet-desktop.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                if output:
                    page.screenshot(path=str(Path(output) / "wallet-mobile.png"), full_page=True)
                assert not errors, errors
                browser.close()
                print(
                    json.dumps(
                        {
                            "browser": "Chromium",
                            "wallet": "passed",
                            "profile_crud": "passed",
                            "secret_rotation": "passed",
                            "stream_and_credit": "passed",
                            "history": "passed",
                            "phase_two_scenarios": 6,
                            "mobile": "passed",
                            "javascript_errors": 0,
                        }
                    )
                )
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
