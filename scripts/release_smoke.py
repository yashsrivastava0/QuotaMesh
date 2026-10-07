"""Clean installed/uvx process acceptance, isolated data and fake upstream only."""

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_ready(client, process):
    for _ in range(600):
        if process.poll() is not None:
            raise AssertionError("Installed command exited before becoming ready")
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("Installed command did not become ready within 60 seconds")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--package")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    if args.wheel or args.package:
        command = [shutil.which("uvx") or "uvx"]
        if args.offline:
            command += ["--offline"]
        if args.cache_dir:
            command += ["--cache-dir", str(args.cache_dir.resolve())]
        command += [
            "--from",
            str(args.wheel.resolve()) if args.wheel else args.package,
            "quotamesh",
        ]
    else:
        command = [shutil.which("quotamesh") or "quotamesh"]
    with tempfile.TemporaryDirectory(prefix="quotamesh-release-") as directory:
        directory = Path(directory)
        env = {**os.environ, "QUOTAMESH_DATA_DIR": str(directory / "data"), "PYTHONPATH": ""}
        port, upstream_port = free_port(), free_port()
        processes = []
        files = []
        process_command = command

        def launch(arguments, label):
            output = (directory / (label + ".log")).open("w", encoding="utf-8")
            files.append(output)
            process = subprocess.Popen(
                process_command + arguments,
                cwd=directory,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            processes.append(process)
            return process

        started = time.monotonic()
        try:
            version = subprocess.run(
                command + ["--version"],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            ).stdout.strip()
            assert version == "0.1.0", version
            if args.wheel or args.package:
                # Resolve the installed console entry point once. Launching it
                # directly keeps process ownership and shutdown deterministic;
                # uvx's wrapper may exit before its Windows child shuts down.
                executable = subprocess.run(
                    command[:-1] + ["python", "-c", "import sys; print(sys.executable)"],
                    cwd=directory,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=True,
                ).stdout.strip()
                entrypoint = Path(executable).parent / (
                    "quotamesh.exe" if os.name == "nt" else "quotamesh"
                )
                assert entrypoint.is_file(), entrypoint
                process_command = [str(entrypoint)]
            launch(["fake-upstream", "--port", str(upstream_port)], "provider")
            gateway = launch(["start", "--port", str(port), "--no-browser"], "gateway")
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5
            ) as client:
                wait_ready(client, gateway)
                key = subprocess.run(
                    command + ["key"],
                    cwd=directory,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=True,
                ).stdout.strip()
                client.headers["Authorization"] = "Bearer " + key
                base_url = f"http://127.0.0.1:{upstream_port}/v1"
                # The fake process exposes /v1/models rather than /health.
                for _ in range(100):
                    try:
                        ready = client.get(
                            base_url + "/models", headers={"Authorization": "Bearer fake-200"}
                        )
                        if ready.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                identifiers = []
                for secret in ("fake-429-short:primary", "fake-200:backup"):
                    saved = client.post(
                        "/api/credentials",
                        json={
                            "provider_id": "custom",
                            "base_url": base_url,
                            "plan_type": "FREE",
                            "secret_value": secret,
                            "quota_group": secret.split(":")[1],
                        },
                    )
                    assert saved.status_code == 201, saved.text
                    identifiers.append(saved.json()["credential_id"])
                assert (
                    client.post(
                        "/api/setup", json={"credential_id": identifiers[0], "model": "fake-free"}
                    ).status_code
                    == 201
                )
                assert (
                    client.post(
                        "/api/policy",
                        json={"targets": [{"provider_id": "custom", "model": "fake-free"}]},
                    ).status_code
                    == 200
                )
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "qm/default",
                        "messages": [{"role": "user", "content": "release-private-prompt"}],
                    },
                )
                assert (
                    response.status_code == 200 and response.headers["x-quotamesh-attempts"] == "2"
                ), response.text
                assert (
                    response.json()["choices"][0]["message"]["content"]
                    == "Hello from fake upstream"
                )
                first_request_s = round(time.monotonic() - started, 2)
                assert first_request_s < 300
                stream = client.post(
                    "/v1/chat/completions",
                    json={"model": "qm/default", "messages": [], "stream": True},
                )
                assert stream.status_code == 200 and "[DONE]" in stream.text
                for path in (
                    "/static/dashboard.js",
                    "/static/style.css",
                    "/static/favicon.svg",
                    "/api/catalog",
                    "/api/explain",
                    "/api/integrations",
                ):
                    assert client.get(path).status_code == 200
                denied = client.post(
                    "/api/policy", json={}, headers={"Origin": "https://foreign.example"}
                )
                assert denied.status_code == 403
                duplicate = launch(
                    ["start", "--port", str(free_port()), "--no-browser"], "duplicate"
                )
                assert duplicate.wait(timeout=15) != 0
                # New owner after graceful shutdown verifies that the OS lock is released.
                if os.name != "nt":
                    gateway.send_signal(signal.SIGINT)
                else:
                    gateway.send_signal(signal.CTRL_BREAK_EVENT)
                gateway.wait(timeout=15)
                gateway = launch(["start", "--port", str(port), "--no-browser"], "restart")
                wait_ready(client, gateway)
                assert client.get("/api/activity").json()["requests"]
                assert (
                    client.get("/api/explain").json()["selected"]["credential_id"] == identifiers[1]
                )
                serialized = client.get("/api/activity").text + client.get("/api/wallet").text
                for secret in (
                    "fake-429-short:primary",
                    "fake-200:backup",
                    "release-private-prompt",
                    "Hello from fake upstream",
                ):
                    assert secret not in serialized
                print(
                    json.dumps(
                        {
                            "version": version,
                            "first_request_s": first_request_s,
                            "installed_processes": "passed",
                            "stream": "passed",
                            "two_keys_one_provider": "passed",
                            "restart": "passed",
                            "single_process_lock": "passed",
                            "privacy": "passed",
                        }
                    )
                )
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.send_signal(
                        signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
                    )
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        if os.name == "nt":
                            subprocess.run(
                                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                capture_output=True,
                                check=False,
                            )
                        else:
                            process.kill()
                        process.wait(timeout=5)
            for output in files:
                output.close()
            for path in directory.glob("*.log"):
                content = path.read_text(encoding="utf-8")
                assert not any(
                    s in content
                    for s in (
                        "fake-429-short:primary",
                        "fake-200:backup",
                        "release-private-prompt",
                        "Hello from fake upstream",
                    )
                ), "Private data in process logs"


if __name__ == "__main__":
    main()
