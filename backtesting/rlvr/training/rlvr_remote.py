"""远程 GPU 机器操作工具 — 通过 HTTP 代理 CONNECT 隧道 + paramiko 密码认证。

用法（命令行）：
    python3 rlvr_remote.py exec "nvidia-smi"
    python3 rlvr_remote.py put local_path remote_path
    python3 rlvr_remote.py get remote_path local_path
    python3 rlvr_remote.py putdir local_dir remote_dir
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time

import paramiko

# 通过环境变量配置远程机器，切勿把凭据提交进仓库：
#   RLVR_SSH_HOST / RLVR_SSH_PORT / RLVR_SSH_USER / RLVR_SSH_PASSWORD
HOST = os.environ.get("RLVR_SSH_HOST", "")
PORT = int(os.environ.get("RLVR_SSH_PORT", "22"))
USER = os.environ.get("RLVR_SSH_USER", "root")
PASSWORD = os.environ.get("RLVR_SSH_PASSWORD", "")
PROXY = (
    os.environ.get("RLVR_PROXY_HOST", "127.0.0.1"),
    int(os.environ.get("RLVR_PROXY_PORT", "18080")),
)

if not HOST or not PASSWORD:
    raise SystemExit("请先设置 RLVR_SSH_HOST 与 RLVR_SSH_PASSWORD 环境变量")


def _open_transport() -> paramiko.Transport:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(30)
    s.connect(PROXY)
    s.sendall(
        f"CONNECT {HOST}:{PORT} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n\r\n".encode()
    )
    resp = s.recv(4096).decode(errors="ignore")
    if "200" not in resp.splitlines()[0]:
        raise RuntimeError(f"proxy CONNECT failed: {resp[:200]}")
    t = paramiko.Transport(s)
    t.start_client(timeout=30)
    t.auth_password(USER, PASSWORD)
    return t


def run(cmd: str, timeout: int = 600) -> tuple[int, str]:
    t = _open_transport()
    try:
        chan = t.open_session()
        chan.settimeout(timeout)
        chan.exec_command(cmd)
        out = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if chan.recv_ready():
                out += chan.recv(65536)
            if chan.exit_status_ready() and not chan.recv_ready():
                break
            time.sleep(0.2)
        while chan.recv_ready():
            out += chan.recv(65536)
        rc = chan.recv_exit_status()
        return rc, out.decode(errors="replace")
    finally:
        t.close()


def put(local: str, remote: str) -> None:
    t = _open_transport()
    try:
        sftp = paramiko.SFTPClient.from_transport(t)
        sftp.put(local, remote)
        sftp.close()
        print(f"[PUT] {local} -> {remote} ({os.path.getsize(local)} B)")
    finally:
        t.close()


def get(remote: str, local: str) -> None:
    t = _open_transport()
    try:
        sftp = paramiko.SFTPClient.from_transport(t)
        sftp.get(remote, local)
        sftp.close()
        print(f"[GET] {remote} -> {local}")
    finally:
        t.close()


def putdir(local_dir: str, remote_dir: str) -> None:
    """递归上传目录（SFTP，逐文件）。"""
    t = _open_transport()
    try:
        sftp = paramiko.SFTPClient.from_transport(t)
        _mkdirs(sftp, remote_dir)
        n = 0
        total = 0
        for root, _, files in os.walk(local_dir):
            rel = os.path.relpath(root, local_dir)
            rdir = remote_dir if rel == "." else f"{remote_dir}/{rel}".replace("\\", "/")
            _mkdirs(sftp, rdir)
            for f in files:
                lp = os.path.join(root, f)
                rp = f"{rdir}/{f}"
                sftp.put(lp, rp)
                n += 1
                total += os.path.getsize(lp)
                if n % 20 == 0:
                    print(f"  ... uploaded {n} files ({total/1e6:.1f} MB)")
        sftp.close()
        print(f"[PUTDIR] {local_dir} -> {remote_dir} : {n} files, {total/1e6:.2f} MB")
    finally:
        t.close()


def _mkdirs(sftp: paramiko.SFTPClient, path: str) -> None:
    parts = path.strip("/").split("/")
    cur = ""
    for p in parts:
        cur = f"{cur}/{p}"
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            sftp.mkdir(cur)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["exec", "put", "get", "putdir"])
    ap.add_argument("a")
    ap.add_argument("b", nargs="?")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()
    if args.action == "exec":
        rc, out = run(args.a, args.timeout)
        print(out)
        sys.exit(rc)
    elif args.action == "put":
        put(args.a, args.b)
    elif args.action == "get":
        get(args.a, args.b)
    elif args.action == "putdir":
        putdir(args.a, args.b)


if __name__ == "__main__":
    main()
