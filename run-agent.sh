export SERVER_BASE_URL=http://localhost:8080

python -m uvicorn app.main:app --host 127.0.0.1 --port 8030 --reload
