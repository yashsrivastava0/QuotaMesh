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


def fresh_start(browser):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with TemporaryDirectory(prefix="quotamesh-onboarding-") as directory:
        app = create_app(Path(directory))
        app.mount("/fake", fake_app)
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
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"http://127.0.0.1:{port}/bootstrap?token={app.state.bootstrap_token}")
            page.wait_for_function(
                "document.querySelector('#explain-summary').textContent.includes('save your default profile')"
            )
            assert page.locator("#credential-list .credential").count() == 0
            form = page.locator("#credential-form")
            form.locator("[name=provider_id]").select_option("custom")
            form.locator("[name=plan_type]").select_option("FREE")
            form.locator("[name=secret_value]").fill("fake-200")
            form.locator("[name=base_url]").fill(f"http://127.0.0.1:{port}/fake/v1")
            page.locator("#save-check").click()
            page.wait_for_function(
                "document.querySelectorAll('#doctor-results button').length === 1 && !document.querySelector('#save-check').disabled"
            )
            assert form.locator("[name=secret_value]").input_value() == ""
            page.locator("#doctor-results select").select_option("fake-free")
            page.locator("#doctor-results button").click()
            page.locator("#policy-form button[type=submit]").click()
            page.wait_for_function(
                "document.querySelector('#explain-summary').textContent.includes('Saved policy: qm/default') && !document.querySelector('#policy-form button[type=submit]').disabled"
            )
            assert not page.locator("#policy-form [name=allow_paid]").is_checked()
            page.locator("#test-form button[type=submit]").click()
            page.wait_for_function(
                "document.querySelector('#test-summary').textContent.includes('HTTP 200') && !document.querySelector('#test-form button[type=submit]').disabled"
            )
            assert "Hello from fake upstream" in page.locator("#test-output").inner_text()
            assert not errors, errors
        finally:
            page.close()
            server.should_exit = True
            thread.join(timeout=10)


def phase_four(page, app, port):
    """New UI paths use explicit actions and keep unsaved edits intact."""
    page.wait_for_function("document.querySelectorAll('#catalog-entries article').length === 6")
    assert "Saved policy: qm/default" in page.locator("#explain-summary").inner_text()
    assert "quotamesh key --data-dir" in page.locator("#integration-code").inner_text()
    page.locator("#integration-shell").select_option("bash")
    page.wait_for_function(
        "document.querySelector('#integration-code').textContent.startsWith('export')"
    )
    page.locator("#integration-client").select_option("opencode")
    config = json.loads(page.locator("#integration-code").inner_text())
    assert config["provider"]["quotamesh"]["npm"] == "@ai-sdk/openai-compatible"
    assert config["provider"]["quotamesh"]["options"]["baseURL"] == f"http://127.0.0.1:{port}/v1"
    # Clipboard permissions are not required to select the snippet for manual copying.
    page.evaluate(
        "() => { navigator.clipboard.writeText = async () => { throw new Error('denied'); }; }"
    )
    page.locator("#copy-integration").click()
    assert "Text selected" in page.locator("#copy-status").inner_text()
    page.evaluate(
        "() => { navigator.clipboard.writeText = async text => { window.copiedSnippet = text; }; }"
    )
    page.locator("#copy-integration").click()
    page.wait_for_function("document.querySelector('#copy-status').textContent === 'Copied.'")
    assert page.evaluate("window.copiedSnippet") == page.locator("#integration-code").inner_text()
    page.locator("#doctor-credential").select_option("2")
    page.locator("#doctor-mode").select_option("models")
    page.locator("#doctor-form button[type=submit]").click()
    page.wait_for_function(
        "document.querySelector('#doctor-status').textContent.includes('listing ok') && !document.querySelector('#doctor-form button[type=submit]').disabled"
    )
    assert "fake-free" in page.locator("#doctor-results").inner_text()
    targets = page.locator("#targets .target-row").count()
    page.locator("#doctor-results button").first.click()
    assert page.locator("#targets .target-row").count() == targets + 1
    page.locator("#targets .target-row").last.locator("[data-field=model]").fill("unsaved-model")
    page.locator("#refresh").click()
    page.wait_for_function("document.querySelector('#notice').textContent.includes('Model added')")
    assert (
        page.locator("#targets .target-row").last.locator("[data-field=model]").input_value()
        == "unsaved-model"
    )
    count = len(app.state.store.recent_attempts())
    page.locator("#doctor-credential").select_option("3")
    page.locator("#doctor-mode").select_option("generation")
    page.locator("#doctor-form button[type=submit]").click()
    assert "Authorize quota" in page.locator("#doctor-status").inner_text()
    assert len(app.state.store.recent_attempts()) == count
    page.locator("#doctor-consent").check()
    page.locator("#doctor-form button[type=submit]").click()
    page.wait_for_function(
        "document.querySelector('#doctor-status').textContent.includes('paid blocked') && !document.querySelector('#doctor-form button[type=submit]').disabled"
    )
    assert len(app.state.store.recent_attempts()) == count
    page.locator("#doctor-credential").select_option("2")
    page.locator("#doctor-consent").check()
    page.locator("#doctor-form button[type=submit]").click()
    page.wait_for_function(
        "document.querySelector('#doctor-status').textContent.includes('Generation: ok') && !document.querySelector('#doctor-form button[type=submit]').disabled"
    )
    assert len(app.state.store.recent_attempts()) == count + 1
    # Change profiles in one browser task so earlier refresh responses can arrive late.
    page.evaluate("""() => {
      const select = document.querySelector('#profile-select');
      select.value = 'free-app'; select.dispatchEvent(new Event('change'));
      select.value = 'paid-backup'; select.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_function(
        "document.querySelector('#connection-alias').textContent === 'qm/paid-backup'"
    )
    assert page.locator("#integration-alias").inner_text() == "qm/paid-backup"
    assert page.locator("#profile-slug").input_value() == "paid-backup"
    page.locator("#profile-select").select_option("default")
    page.wait_for_function(
        "document.querySelector('#connection-alias').textContent === 'qm/default'"
    )
    page.locator("#preview-env").click()
    page.wait_for_function("document.querySelectorAll('#environment-results input').length === 5")
    assert "OPENAI_API_KEY" in page.locator("#environment-results").inner_text()
    assert not any(
        secret in page.content() for secret in ("fake-200", "fake-paid", '"secret_value":')
    )
    # Add/Test saves first, clears the secret form, then runs listing only.
    form = page.locator("#credential-form")
    form.locator("[name=provider_id]").select_option("custom")
    form.locator("[name=plan_type]").select_option("FREE")
    form.locator("[name=secret_value]").fill("fake-200")
    form.locator("[name=base_url]").fill(app.state.demo_base_url)
    page.locator("#save-check").click()
    page.wait_for_function(
        "document.querySelectorAll('#credential-list .credential').length === 6 && document.querySelector('#doctor-status').textContent.includes('listing ok') && !document.querySelector('#save-check').disabled"
    )
    assert form.locator("[name=secret_value]").input_value() == ""
    for width in (390, 768, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.set_viewport_size({"width": 1280, "height": 900})
    page.locator(".skip-link").focus()
    assert page.locator(".skip-link").evaluate(
        "element => element.getBoundingClientRect().top >= 0"
    )
    page.locator("#doctor").screenshot(
        path=str(Path(os.environ.get("QM_SCREENSHOTS", "output/phase-four")) / "doctor.png")
    ) if os.environ.get("QM_SCREENSHOTS") else None


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
                fresh_start(browser)
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
                phase_four(page, app, port)
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
                            "explain_connect_doctor": "passed",
                        }
                    )
                )
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
