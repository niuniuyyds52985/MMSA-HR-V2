"""Start this checkout's V2 workbench, reusing an up-to-date local service."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener
import webbrowser

from .access import PROJECT, data_root, operator_key


def build_id():
    digest = hashlib.sha256()
    files = [*Path(__file__).parent.glob("*.py"), *(PROJECT / "web/v2").rglob("*"),
             *(PROJECT / "config/v2").glob("*.json")]
    for path in sorted(files):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def run_server(port):
    local_ffmpeg = PROJECT / ".runtime" / "ffmpeg" / "bin"
    if (local_ffmpeg / "ffmpeg.exe").is_file() and (local_ffmpeg / "ffprobe.exe").is_file():
        os.environ["PATH"] = str(local_ffmpeg) + os.pathsep + os.environ.get("PATH", "")
    # WinGet exposes executable links. Subprocesses launched by the V1 model
    # adapters need to find the real binary and the ffprobe installed beside it.
    if os.name == "nt":
        ffmpeg = shutil.which("ffmpeg") or str(Path(os.environ.get("LOCALAPPDATA","")) / "Microsoft/WinGet/Links/ffmpeg.exe")
        try:
            binary = Path(ffmpeg).resolve(strict=True)
        except OSError:
            binary = None
        if binary and (binary.parent / "ffprobe.exe").is_file():
            os.environ["PATH"] = str(binary.parent) + os.pathsep + os.environ.get("PATH","")
    import uvicorn
    from .api import app
    revision = build_id()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))

    @app.get("/v2/api/runtime")
    def runtime():
        return {"service":"mmsa-v2-local", "build":revision}

    @app.post("/v2/api/runtime/shutdown")
    def stop_local_service():
        server.should_exit = True
        return {"stopping":True}
    server.run()


@contextmanager
def startup_lock(root, port, timeout=25):
    """Serialize local launcher/MCP startup without changing global state."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / f".startup-{port}.lock").open("a+b") as handle:
        deadline = time.monotonic() + timeout
        acquired = False
        try:
            while not acquired:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("另一个窗口正在启动工作台，请稍后重试") from None
                    time.sleep(.1)
            yield
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def start(port, open_browser, *, announce=True, restart_stale=True):
    with startup_lock(data_root(), port):
        return _start(port, open_browser, announce=announce, restart_stale=restart_stale)


def _start(port, open_browser, *, announce=True, restart_stale=True):
    base = f"http://127.0.0.1:{port}"
    root = data_root()
    key = operator_key(root)
    opener = build_opener(ProxyHandler({}))

    def request(resource, method="GET"):
        req = Request(base + "/v2/api/runtime" + resource, method=method, headers={"Authorization":"Bearer "+key})
        try:
            with opener.open(req, timeout=2) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError("端口正在被其他服务或其他数据目录使用") from exc
        except (URLError, OSError):
            return None

    revision = build_id()
    health = request("")
    if health and health.get("service") != "mmsa-v2-local":
        raise RuntimeError("端口被其他服务占用")
    if health and health.get("build") != revision and restart_stale:
        request("/shutdown","POST")
        for _ in range(100):
            time.sleep(.15)
            if not request(""):
                break
        else:
            raise RuntimeError("旧服务正在处理任务，请稍后再打开工作台")
        health = None
    if not health:
        with (root / "server.log").open("ab") as log:
            process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "src.mmsa_v2.launch",
                                        "--serve", "--port", str(port)], cwd=PROJECT, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError("服务启动失败，请查看本机 V2 数据目录的 server.log")
            health = request("")
            if health:
                break
            time.sleep(.25)
        else:
            raise RuntimeError("V2 服务未及时响应")
    if open_browser and not webbrowser.open(base+"/v2/#access="+key):
        raise RuntimeError("请用本机启动器重新打开浏览器")
    result = {"ready": True, "workbench_url": base + "/v2/", "build": health.get("build"),
              "update_available": health.get("build") != revision}
    if announce:
        print(f"V2 工作台就绪 {base}/v2/ （{result['build']}）")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port",type=int,default=8767)
    parser.add_argument("--serve",action="store_true")
    parser.add_argument("--open",action="store_true")
    flags = parser.parse_args()
    if not 1 <= flags.port <= 65535:
        parser.error("无效服务端口")
    if flags.serve:
        run_server(flags.port)
    else:
        start(flags.port,flags.open)
