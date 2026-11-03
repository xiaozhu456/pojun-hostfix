#!/usr/bin/env python3
"""Exercise real built programs in a disposable PG database and application dir."""
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def run(args, **kw):
    return subprocess.run(args, check=True, **kw)


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(base, route, keyfile):
    def enc(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")
    body = enc(b'{"alg":"HS256","typ":"JWT"}') + b"." + enc(json.dumps({"sub": "POJUN", "exp": int(time.time()) + 60}).encode())
    token = (body + b"." + enc(hmac.new(keyfile.read_bytes().strip(), body, hashlib.sha256).digest())).decode()
    req = urllib.request.Request(base + route, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def wait_ready(proc, base, keyfile, expected):
    deadline = time.monotonic() + 90
    last = "not started"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("application supervisor exited: " + str(proc.returncode))
        try:
            found = request(base, "/api/version", keyfile)
            if found.get("version") == expected:
                tools = request(base, "/api/tools/inventory", keyfile)["tools"]
                by_id = {item["id"]: item for item in tools}
                if not by_id["pika"]["available"] or not by_id["seclists"]["available"]:
                    raise RuntimeError("detection patch was lost across an upgrade")
                return
            last = repr(found)
        except (OSError, urllib.error.URLError, ValueError) as error:
            last = str(error)
        time.sleep(0.1)
    raise RuntimeError("version did not settle at " + expected + ": " + last)


def stop(proc):
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    match = re.fullmatch(r"v?([0-9]+)\.([0-9]+)\.([0-9]+)-hostfix\.([1-9][0-9]*)", args.version)
    if not match or sys.platform != "linux":
        parser.error("requires a Linux hostfix build")
    major, minor, patch, revision = map(int, match.groups())
    first = args.version.removeprefix("v")
    second = f"{major}.{minor}.{patch}-hostfix.{revision + 1}"
    third = f"{major}.{minor}.{patch + 1}-hostfix.1"
    dsn = os.environ.get("POJUN_PG_DSN", "")
    parsed = urllib.parse.urlparse(dsn)
    if parsed.hostname not in ("127.0.0.1", "localhost") or parsed.port != 15432:
        raise ValueError("upgrade smoke tests require the isolated PostgreSQL port 15432")
    database = "pojun_upgrade_" + str(os.getpid())
    pg_env = {**os.environ, "PGPASSWORD": urllib.parse.unquote(parsed.password or ""), "PGSSLMODE": "disable"}
    # PGDATABASE does not expand connection URIs on every libpq path. Pass the
    # isolated endpoint explicitly so psql can never use a default host/socket.
    psql = ["psql", "-h", parsed.hostname, "-p", str(parsed.port),
            "-U", urllib.parse.unquote(parsed.username or "pojun"), "-d", "postgres", "-v", "ON_ERROR_STOP=1"]
    run([*psql, "-c", 'CREATE DATABASE "' + database + '"'], env=pg_env, stdout=subprocess.DEVNULL)
    test_dsn = urllib.parse.urlunparse(parsed._replace(path="/" + database))
    try:
        with tempfile.TemporaryDirectory(prefix="pojun-upgrade-") as temp:
            root = Path(temp)
            binaries = {}
            for v in [first, second, third]:
                path = root / ("fixture-" + v)
                run(["go", "build", "-tags", "embedui", "-ldflags",
                     "-s -w -X main.version=" + v + " -X github.com/chu0119/pojun/selfupdate.Repo=xiaozhu456/pojun-hostfix",
                     "-o", str(path), "./cmd/pojun"], cwd=ROOT)
                binaries[v] = path
            app = root / "app"
            app.mkdir()
            shutil.copyfile(binaries[first], app / "pojun")
            (app / "pojun").chmod(0o755)
            shutil.copyfile(ROOT / "start.sh", app / "start.sh")
            modules = root / "modules"
            modules.mkdir()
            (modules / "pika.py").write_text("# importable fixture without a CLI\n")
            words = root / "words/Discovery/Web-Content"
            words.mkdir(parents=True)
            (words / "common.txt").write_text("fixture\n")
            web_port, proxy_port = port(), port()
            while proxy_port == web_port:
                proxy_port = port()
            base = "http://127.0.0.1:" + str(web_port)
            env = {**os.environ, "POJUN_PG_DSN": test_dsn, "HOME": str(root),
                   "POJUN_TOOL_PYTHON": sys.executable, "PYTHONPATH": str(modules),
                   "POJUN_SECLISTS_DIR": str(root / "words"),
                   "POJUN_HOSTD_SOCKET": str(root / "absent-hostd.sock")}
            command = ["sh", "start.sh", "-addr", "127.0.0.1:" + str(web_port),
                       "-proxy", "127.0.0.1:" + str(proxy_port), "-data", str(app / "data")]
            with (root / "application.log").open("wb") as log:
                def start(expected):
                    proc = subprocess.Popen(command, cwd=app, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    try:
                        wait_ready(proc, base, app / "jwt.key", expected)
                    except Exception:
                        stop(proc)
                        print((root / "application.log").read_text(errors="replace")[-5000:])
                        raise
                    return proc

                proc = start(first)
                stop(proc)
                for old, new in [(first, second), (second, third)]:
                    shutil.copyfile(binaries[new], app / "pojun.new")
                    (app / "pojun.new").chmod(0o755)
                    (app / "pojun.new.sha256").write_text(hashlib.sha256((app / "pojun.new").read_bytes()).hexdigest())
                    (app / "pojun.upgrade.json").write_text(json.dumps({"from": old, "to": new, "attempts": 0, "staged_at": int(time.time())}))
                    proc = start(new)
                    stop(proc)
                    print("verified isolated upgrade", old, "->", new, flush=True)
                # The unmodified current program must survive a staged bad hash.
                shutil.copyfile(binaries[second], app / "pojun.new")
                (app / "pojun.new.sha256").write_text("0" * 64)
                proc = start(third)
                stop(proc)
                if (app / "pojun.new").exists():
                    raise RuntimeError("corrupt staged program was not discarded")
                print("verified corrupt update rejected; detection and running version retained", flush=True)
                (app / "pojun.new").write_bytes(b"not an executable\n")
                (app / "pojun.new").chmod(0o755)
                (app / "pojun.new.sha256").write_text(hashlib.sha256((app / "pojun.new").read_bytes()).hexdigest())
                proc = start(third)
                stop(proc)
                if (app / "pojun.new").exists():
                    raise RuntimeError("unstartable staged program was not discarded")
                print("verified startup smoke failure preserves the running version", flush=True)
    finally:
        run([*psql, "-c", 'DROP DATABASE "' + database + '" WITH (FORCE)'], env=pg_env, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
