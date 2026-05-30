@echo off
echo ==============================
echo  Code Review Agent + ngrok
echo ==============================
echo.
echo Starting review service...
start "Review Service" cmd /c "cd /d "D:\AI program\code-review-agent" && call .venv\Scripts\activate && uvicorn app.main:app --host 127.0.0.1 --port 8000"

timeout /t 3 /nobreak >nul
echo Starting ngrok tunnel...
start "ngrok" cmd /c "ngrok http 8000"

echo.
echo Both started. Check the two new windows.
echo Webhook URL: https://YOUR-NGROK.ngrok-free.dev/webhook
echo Health: http://127.0.0.1:8000/health
echo.
pause
