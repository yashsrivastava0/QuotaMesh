"""Record the task-page walkthrough using temporary synthetic demo data."""

import os
import time
from pathlib import Path

from browser_smoke import go, load, local_server, ready, send
from playwright.sync_api import sync_playwright


def main():
    output = Path("output/phase-five")
    output.mkdir(parents=True, exist_ok=True)
    with local_server(True) as (app, origin), sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH"))
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            record_video_dir=str(output / "recordings"),
            record_video_size={"width": 1280, "height": 900},
        )
        errors = []
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"{origin}/bootstrap?token={app.state.bootstrap_token}")
            ready(page)
            time.sleep(2)
            load(page, origin, "fallback")
            assert "2 upstream attempt" in send(page)
            time.sleep(2)
            load(page, origin, "paid-guard")
            assert "HTTP 429" in send(page)
            time.sleep(2)
            go(page, origin, "/profiles")
            page.locator("[name=allow_paid]").check()
            page.locator("[name=paid_daily_cap_usd]").fill("1")
            page.locator("#policy-form button[type=submit]").click()
            page.wait_for_function(
                "document.querySelector('#notice').textContent.includes('Profile saved')"
            )
            time.sleep(2)
            go(page, origin, "/route")
            assert page.locator("#decisions .eligible").count()
            time.sleep(2)
            go(page, origin, "/connect")
            page.locator("#integration-client").select_option("python")
            time.sleep(2)
            assert not errors, errors
            video = page.video
        finally:
            context.close()
        path = output / "quotamesh-final.webm"
        video.save_as(str(path))
        browser.close()
        print(path.resolve())


if __name__ == "__main__":
    main()
