# 注册每日 09:00 的租房采集定时任务（需管理员运行，由 安装定时任务.bat 自动提权）
$ErrorActionPreference = "Stop"
$roomDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python).Source

$action = New-ScheduledTaskAction -Execute $py -Argument "run.py daily --headless" -WorkingDirectory $roomDir
$trigger = New-ScheduledTaskTrigger -Daily -At 9:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "RoomRentalDaily" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "每日自动采集北京租房房源（贝壳/自如/58/豆瓣等），评分后在 room 目录生成 Excel 并桌面通知" -Force | Out-Null

Write-Host ""
Write-Host "==== 安装成功 ====" -ForegroundColor Green
Write-Host "定时任务 RoomRentalDaily：每天 09:00 自动执行（仅在你登录电脑时运行）"
Write-Host "任务动作: $py run.py daily --headless"
Write-Host "工作目录: $roomDir"
Write-Host ""
Write-Host "卸载命令(管理员): schtasks /delete /tn RoomRentalDaily /f"
Start-Sleep -Seconds 4
