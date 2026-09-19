@echo off
chcp 65001 >nul
title 免费节点配置生成器
cd /d "%~dp0"

:: ============================================================
::  本地一键生成免费节点配置（Windows）
::  用法：双击本文件即可。
::
::  默认：不做测速，只抓取+解析+生成（快，和云端行为一致）。
::  想生成「测速过滤 + 按延迟排序」的配置（更干净但慢几分钟），
::  把下面 EXTRA 那行改成：
::      set EXTRA=--test --limit 300
:: ============================================================

set EXTRA=

:: 取当天日期（yyyyMMdd），输出文件按日期命名，不会互相覆盖
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set TODAY=%%i

echo 正在抓取节点并生成配置（%TODAY%）...
python fetch_nodes.py --out "clash_config_%TODAY%.yaml" %EXTRA%

if errorlevel 1 (
  echo.
  echo [出错] 生成失败。请确认：
  echo   1. 电脑上已安装 Python 3（安装后在命令行输入 python --version 有版本号）
  echo   2. 当前网络能访问节点源（GitHub / Telegram，挂了代理更稳）
  echo.
) else (
  echo.
  echo 完成！生成文件：
  echo   clash_config_%TODAY%.yaml        (Clash Meta 格式)
  echo   clash_config_%TODAY%_v2ray.txt   (v2rayN / v2rayNG 通用订阅格式)
  echo.
  echo 提示：把这几个文件拷到手机，或传到网盘，就能在 Clash 里导入。
)

echo.
pause
