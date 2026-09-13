#!/usr/bin/env python3
"""ssh_helper.py — paramiko-based SSH/SFTP to remote GPU machine."""
import paramiko, sys, time, os

# 通过环境变量配置远程机器，切勿把凭据提交进仓库：
#   RLVR_SSH_HOST / RLVR_SSH_PORT / RLVR_SSH_USER / RLVR_SSH_PASSWORD
HOST = os.environ.get("RLVR_SSH_HOST", "")
PORT = int(os.environ.get("RLVR_SSH_PORT", "22"))
USER = os.environ.get("RLVR_SSH_USER", "root")
PASS = os.environ.get("RLVR_SSH_PASSWORD", "")
PROXY = os.environ.get(
    "RLVR_SSH_PROXY_COMMAND",
    f"nc -X connect -x {os.environ.get('RLVR_PROXY_HOST', '127.0.0.1')}:{os.environ.get('RLVR_PROXY_PORT', '18080')} %h %p",
)

if not HOST or not PASS:
    raise SystemExit("请先设置 RLVR_SSH_HOST 与 RLVR_SSH_PASSWORD 环境变量")

def _make_client():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    sock = paramiko.ProxyCommand(PROXY.replace("%h", HOST).replace("%p", str(PORT)))
    for attempt in range(5):
        try:
            client.connect(HOST, port=PORT, username=USER, password=PASS,
                           sock=sock, timeout=30, allow_agent=False, look_for_keys=False)
            return client
        except Exception as e:
            if attempt == 4:
                raise
            time.sleep(2 + attempt * 2)

def run(cmd, timeout=600):
    """Run a command, return (stdout, stderr, exit_code)."""
    client = _make_client()
    try:
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout, get_pty=False)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return out, err, code
    finally:
        client.close()

def run_bg(cmd, log_path):
    """Run a command in background using nohup + screen-like via nohup."""
    # Use setsid + nohup to detach
    wrapped = f"cd /root/Pronoia && setsid bash -c '{cmd}' > {log_path} 2>&1 < /dev/null & echo $!"
    out, err, code = run(wrapped, timeout=30)
    return out, err, code

def sftp_upload(local, remote):
    client = _make_client()
    try:
        sftp = client.open_sftp()
        # ensure remote dir exists
        rdir = remote.rsplit("/", 1)[0]
        try:
            sftp.stat(rdir)
        except FileNotFoundError:
            run(f"mkdir -p {rdir}")
        sftp.put(local, remote)
        sftp.close()
    finally:
        client.close()

def sftp_download(remote, local):
    client = _make_client()
    try:
        sftp = client.open_sftp()
        sftp.get(remote, local)
        sftp.close()
    finally:
        client.close()

if __name__ == "__main__":
    cmd = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "echo ok"
    out, err, code = run(cmd)
    print(out)
    if err:
        print("STDERR:", err[:2000], file=sys.stderr)
    sys.exit(code)
