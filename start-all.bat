@echo off
echo ==============================
echo  Code Review Agent + ngrok
echo ==============================
echo.

echo [1/2] Starting review service on port 8000...
start "Code Review Agent" cmd /k "cd /d "D:\AI program\code-review-agent" && call .venv\Scripts\activate && uvicorn app.main:app --host 127.0.0.1 --port 8000"

echo Waiting for service to be ready...
:wait
timeout /t 2 /nobreak >nul
curl -s http://127.0.0.1:8000/health >nul 2>&1
if errorlevel 1 goto wait

echo [2/2] Starting ngrok...
start "ngrok" ngrok http 8000

echo.
echo ==============================
echo  Both services started!
echo ==============================
echo.
echo  Health check: http://127.0.0.1:8000/health
echo  ngrok status: http://127.0.0.1:4040
echo  Webhook URL : https://YOUR-NGROK.ngrok-free.dev/webhook
echo               ^^ check the ngrok window for exact URL
echo.
pause
