#!/usr/bin/env python3
"""remote_ssh.py — 通过 HTTP 代理在远程 GPU 机器上执行命令（paramiko）。

用法：
    python3 remote_ssh.py "nvidia-smi"
    python3 remote_ssh.py --put local_path remote_path
    python3 remote_ssh.py --get remote_path local_path
"""
import argparse
import os
import socket
import sys

import paramiko

# 通过环境变量配置远程机器，切勿把凭据提交进仓库：
#   RLVR_SSH_HOST / RLVR_SSH_PORT / RLVR_SSH_USER / RLVR_SSH_PASSWORD
HOST = os.environ.get("RLVR_SSH_HOST", "")
PORT = int(os.environ.get("RLVR_SSH_PORT", "22"))
USER = os.environ.get("RLVR_SSH_USER", "root")
PASSWORD = os.environ.get("RLVR_SSH_PASSWORD", "")
PROXY_HOST = os.environ.get("RLVR_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("RLVR_PROXY_PORT", "18080"))

if not HOST or not PASSWORD:
    raise SystemExit("请先设置 RLVR_SSH_HOST 与 RLVR_SSH_PASSWORD 环境变量")


def _open_transport():
    if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect((PROXY_HOST, PROXY_PORT))
        s.sendall(f"CONNECT {HOST}:{PORT} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n\r\n".encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                raise RuntimeError("proxy closed")
            resp += chunk
        if b" 200 " not in resp.split(b"\r\n")[0]:
            raise RuntimeError(f"proxy CONNECT failed: {resp[:200]!r}")
    else:
        s = socket.create_connection((HOST, PORT), timeout=30)
    t = paramiko.Transport(s)
    t.set_keepalive(30)
    t.connect(username=USER, password=PASSWORD)
    return t


def run(cmd: str, timeout: int = 600) -> int:
    t = _open_transport()
    try:
        ch = t.open_session()
        ch.settimeout(timeout)
        ch.set_combine_stderr(True)
        ch.exec_command(cmd)
        out = b""
        while True:
            if ch.recv_ready():
                out += ch.recv(65536)
            if ch.exit_status_ready() and not ch.recv_ready():
                break
        while ch.recv_ready():
            out += ch.recv(65536)
        rc = ch.recv_exit_status()
        sys.stdout.write(out.decode("utf-8", "replace"))
        return rc
    finally:
        t.close()


def put(local: str, remote: str) -> None:
    t = _open_transport()
    try:
        sftp = paramiko.SFTPClient.from_transport(t)
        sftp.put(local, remote)
        print(f"PUT {local} -> {remote} OK")
        sftp.close()
    finally:
        t.close()


def get(remote: str, local: str) -> None:
    t = _open_transport()
    try:
        sftp = paramiko.SFTPClient.from_transport(t)
        sftp.get(remote, local)
        print(f"GET {remote} -> {local} OK")
        sftp.close()
    finally:
        t.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", help="shell 命令")
    ap.add_argument("--put", nargs=2, metavar=("LOCAL", "REMOTE"))
    ap.add_argument("--get", nargs=2, metavar=("REMOTE", "LOCAL"))
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args()
    if a.put:
        put(a.put[0], a.put[1])
    elif a.get:
        get(a.get[0], a.get[1])
    elif a.cmd:
        sys.exit(run(a.cmd, a.timeout))
    else:
        ap.print_help()
