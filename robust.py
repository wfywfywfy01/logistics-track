#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""健壮性公共模块: 原子写 JSON / 损坏兜底 / 文件锁 / 防注入的子进程调用"""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path


# ---------- 原子写: 先写临时文件再替换, 进程中途死掉也不会写坏 ----------
def atomic_write_json(path, obj):
    path = str(path)
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(suffix=".tmp", dir=d or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False


# ---------- 读 JSON: 损坏时回退 .bak ----------
def load_json_guarded(path, default):
    path = str(path)
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        try:
            return json.load(open(path + ".bak", encoding="utf-8"))
        except Exception:
            return default


def save_json_guarded(path, obj):
    """写前自动把旧文件备份为 .bak, 再原子写"""
    path = str(path)
    try:
        if os.path.exists(path):
            try:
                os.replace(path, path + ".bak")
            except Exception:
                pass
    except Exception:
        pass
    return atomic_write_json(path, obj)


# ---------- 文件锁: 多进程(容器里多脚本)并发写台账互斥 ----------
class FileLock:
    def __init__(self, path, timeout=60):
        self.path = str(path)
        self.timeout = timeout
        self.handle = None

    def acquire(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self.handle = open(self.path, "a+b")
                self.handle.seek(0, os.SEEK_END)
                if self.handle.tell() == 0:
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except (OSError, BlockingIOError):
                if self.handle:
                    self.handle.close()
                    self.handle = None
                if time.time() > deadline:
                    return False
                time.sleep(0.5)

    def release(self):
        if not self.handle:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError("lock timeout: " + self.path)
        return self

    def __exit__(self, *a):
        self.release()


def _pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


# ---------- 子进程: Linux 用参数列表(防注入/引号坑), Windows 回退 shell ----------
def cli_run(args, timeout=120):
    """args: vertu-cli 参数列表, 如 ["im","+bots"]"""
    if sys.platform == "win32":
        cmd = "vertu-cli " + " ".join('"%s"' % a for a in args)
        r = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
    else:
        r = subprocess.run(["vertu-cli"] + list(args), capture_output=True, timeout=timeout)
    return r.returncode, r.stdout.decode("utf-8", errors="replace"), r.stderr.decode("utf-8", errors="replace")
