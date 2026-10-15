@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo    北京租房助手 - 每日采集
echo    浏览器会自动打开翻页，请勿手动关闭
echo    跑完右下角弹窗通知并自动打开 Excel
echo    日志文件: logs\spider.log
echo ============================================
python run.py daily
echo.
echo 执行结束，按任意键关闭窗口
pause >nul
