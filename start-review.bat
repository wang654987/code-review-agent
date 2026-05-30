@echo off
cd /d "D:\AI program\code-review-agent"
call .venv\Scripts\activate
echo Starting Code Review Agent on http://127.0.0.1:8000
uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
