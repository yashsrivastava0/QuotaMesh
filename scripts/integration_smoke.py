"""Execute generated client templates against an isolated fake-provider gateway.

Run with the installed wheel's Python. Verification-only packages: openai (Python),
openai and @ai-sdk/openai-compatible in output/phase-four/node-client (npm).
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import uvicorn

import quotamesh
from quotamesh.app import create_app
from quotamesh.demo import app as fake_app
from quotamesh.demo import configure_demo


def run(command, environment, directory):
    result = subprocess.run(
        command,
        env=environment,
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Hello from fake upstream" in result.stdout, result.stdout


def main():
    output = Path("output/phase-four").resolve()
    node_dir = output / "node-client"
    assert (node_dir / "node_modules/openai").exists(), "Install the verification Node SDKs first"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with TemporaryDirectory(prefix="quotamesh-sdk-") as directory:
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
        environment = {
            **os.environ,
            "QUOTAMESH_BASE_URL": f"http://127.0.0.1:{port}/v1",
            "QUOTAMESH_MODEL": "qm/default",
            "QUOTAMESH_KEY": app.state.local_key,
            "QUOTAMESH_DATA_DIR": directory,
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        }
        environment.pop("PYTHONPATH", None)
        verified = []
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                trust_env=False,
                headers={"Authorization": "Bearer " + app.state.local_key},
            ) as client:
                templates = client.get("/api/integrations?profile=default&shell=bash").json()
                python_file = output / "generated-client.py"
                python_file.write_text(templates["snippets"]["python"], encoding="utf-8")
                run([sys.executable, str(python_file)], environment, output)
                verified.append("Python OpenAI SDK")
                python_file.write_text(
                    """import os
from openai import OpenAI
client = OpenAI(base_url=os.environ['QUOTAMESH_BASE_URL'],api_key=os.environ['QUOTAMESH_KEY'],max_retries=0)
stream = client.chat.completions.create(model='qm/default',messages=[{'role':'user','content':'Hello'}],stream=True)
print(''.join(chunk.choices[0].delta.content or '' for chunk in stream if chunk.choices))
""",
                    encoding="utf-8",
                )
                run([sys.executable, str(python_file)], environment, output)
                verified.append("Python streaming")
                node_file = node_dir / "generated-client.mjs"
                node_file.write_text(templates["snippets"]["node"], encoding="utf-8")
                run(["node", str(node_file)], environment, node_dir)
                verified.append("Node OpenAI SDK")
                bash = (
                    shutil.which("bash") if os.name != "nt" else "C:/Program Files/Git/bin/bash.exe"
                )
                assert bash and Path(bash).exists(), "A real Bash runtime is required"
                run(
                    [
                        bash,
                        "-c",
                        templates["snippets"]["env"] + "\n" + templates["snippets"]["curl"],
                    ],
                    environment,
                    output,
                )
                verified.append("Bash environment + cURL")
                if os.name == "nt":
                    powershell = client.get(
                        "/api/integrations?profile=default&shell=powershell"
                    ).json()
                    run(
                        [
                            "powershell",
                            "-NoProfile",
                            "-NonInteractive",
                            "-Command",
                            powershell["snippets"]["env"]
                            + "\n"
                            + powershell["snippets"]["curl"]
                            + " | ConvertTo-Json -Depth 10",
                        ],
                        environment,
                        output,
                    )
                    verified.append("PowerShell environment + HTTP")
                (node_dir / "opencode.json").write_text(
                    templates["snippets"]["opencode"], encoding="utf-8"
                )
                node_file.write_text(
                    """import fs from 'node:fs';
import {createOpenAICompatible} from '@ai-sdk/openai-compatible';
const config = JSON.parse(fs.readFileSync('opencode.json','utf8'));
const definition = config.provider.quotamesh;
const provider = createOpenAICompatible({name:'quotamesh',baseURL:definition.options.baseURL,apiKey:process.env.QUOTAMESH_KEY});
const result = await provider(Object.keys(definition.models)[0]).doGenerate({prompt:[{role:'user',content:[{type:'text',text:'Hello'}]}]});
console.log(result.content.filter(part => part.type === 'text').map(part => part.text).join(''));
""",
                    encoding="utf-8",
                )
                run(["node", str(node_file)], environment, node_dir)
                verified.append("OpenCode-compatible transport")
                page = client.get(
                    "/bootstrap", params={"token": app.state.bootstrap_token}, follow_redirects=True
                )
                assert page.status_code == 200
                page = client.get("/connect")
                assert page.status_code == 200 and 'id="connect"' in page.text
                assert "/static/dashboard.js" in page.text
                for asset in ("dashboard.js", "style.css", "favicon.svg"):
                    assert client.get("/static/" + asset).status_code == 200
                assert client.get("/api/catalog").status_code == 200
                assert (
                    client.post("/api/doctor", json={"credential_id": 2, "mode": "models"}).json()[
                        "outcome"
                    ]
                    == "listing_ok"
                )
                cli = str(
                    Path(sys.executable).parent
                    / ("quotamesh.exe" if os.name == "nt" else "quotamesh")
                )
                for args in (["profile", "test", "default"], ["doctor", "2"], ["env", "default"]):
                    response = subprocess.run(
                        [cli, *args, "--port", str(port)],
                        env=environment,
                        cwd=output,
                        capture_output=True,
                        text=True,
                        timeout=20,
                        check=False,
                    )
                    assert response.returncode == 0, response.stderr
                verified.append("Installed CLI and package assets")
            print(json.dumps({"package": quotamesh.__file__, "verified": verified}))
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
