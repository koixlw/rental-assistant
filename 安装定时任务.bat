@echo off
chcp 65001 >nul
echo 即将申请管理员权限来注册每日定时任务（每天09:00自动采集房源）
echo 在弹出的 UAC 窗口请点击"是"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','\"%~dp0install_task.ps1\"' -Wait"
echo.
echo 安装流程结束（如上方显示"安装成功"即完成）
pause
