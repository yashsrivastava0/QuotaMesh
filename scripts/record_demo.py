"""Record a short synthetic final MVP walkthrough; no provider secrets or real quota."""

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
    output = Path("output/phase-five")
    output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with TemporaryDirectory(prefix="quotamesh-recording-") as directory:
        app = create_app(Path(directory))
        app.state.demo_mode = True
        app.state.demo_base_url = f"http://127.0.0.1:{port}/demo-upstream/v1"
        configure_demo(app, app.state.demo_base_url, "fallback")
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
                browser = playwright.chromium.launch(
                    executable_path=os.environ.get("CHROMIUM_PATH")
                )
                context = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    record_video_dir=str(output / "recordings"),
                    record_video_size={"width": 1280, "height": 900},
                )
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}/bootstrap?token={app.state.bootstrap_token}")
                page.wait_for_function(
                    "document.querySelectorAll('#credential-list .credential').length === 3"
                )
                time.sleep(3)
                page.locator("#test").scroll_into_view_if_needed()
                time.sleep(2)
                page.locator("#test-form button[type=submit]").click()
                page.wait_for_function(
                    "document.querySelector('#test-summary').textContent.includes('HTTP 200') && !document.querySelector('#test-form button[type=submit]').disabled"
                )
                assert "2 upstream attempt" in page.locator("#test-summary").inner_text()
                time.sleep(3)
                page.locator("#demo-form [name=scenario]").select_option("paid-guard")
                page.locator("#demo-form button").click()
                page.wait_for_function(
                    "document.querySelector('#test-summary').textContent === 'No test sent in this scenario yet.'"
                )
                page.locator("#test").scroll_into_view_if_needed()
                page.locator("#test-form button[type=submit]").click()
                page.wait_for_function(
                    "document.querySelector('#test-summary').textContent.includes('HTTP 429') && !document.querySelector('#test-form button[type=submit]').disabled"
                )
                assert "Paid use is off" in page.locator("#decisions").inner_text()
                time.sleep(3)
                page.locator("#policy").scroll_into_view_if_needed()
                page.locator("#policy-form [name=allow_paid]").check()
                page.locator("#policy-form [name=paid_daily_cap_usd]").fill("1")
                page.locator("#policy-form button[type=submit]").click()
                page.wait_for_function(
                    "!document.querySelector('#policy-form button[type=submit]').disabled"
                )
                time.sleep(2)
                page.locator("#test").scroll_into_view_if_needed()
                page.wait_for_function(
                    "[...document.querySelectorAll('#decisions .eligible')].some(e => e.textContent.includes('Paid'))"
                )
                time.sleep(3)
                page.locator("#integration-client").select_option("python")
                page.locator("#connect").scroll_into_view_if_needed()
                time.sleep(3)
                assert not errors, errors
                video = page.video
                context.close()
                path = output / "quotamesh-final.webm"
                Path(video.path()).replace(path)
                browser.close()
                print(path.resolve())
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
