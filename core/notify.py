"""Windows 桌面气泡通知 + 自动打开文件；非 Windows 或失败时静默降级。"""
from __future__ import annotations

import os
import subprocess
import sys


def notify(title: str, message: str, timeout: int = 40) -> None:
    """右下角气泡通知（任务计划"仅用户登录时运行"下可正常弹出）。"""
    if sys.platform != "win32":
        print(f"[notify] {title}: {message}")
        return
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.BalloonTipTitle = $env:RT_TITLE;"
        "$n.BalloonTipText = $env:RT_MSG;"
        "$n.Visible = $true;"
        "$n.ShowBalloonTip(15000);"
        "Start-Sleep -Seconds 6;"
        "$n.Dispose()"
    )
    try:
        env = dict(os.environ)
        env["RT_TITLE"] = title[:63]
        env["RT_MSG"] = message[:255]
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            env=env, timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        print(f"[notify] {title}: {message}")


def open_file(path: str) -> None:
    """用系统默认程序打开文件（Excel/文件夹等）。"""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception:
        print(f"[notify] 无法自动打开，请手动打开: {path}")
