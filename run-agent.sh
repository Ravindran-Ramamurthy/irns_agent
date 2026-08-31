export SERVER_BASE_URL=http://localhost:8080
export DATA_BASE_PATH=$HOME/bksystems
export KNOWLEDGE_DOMAIN_NAME=IRNS

python -m uvicorn app.main:app --host 127.0.0.1 --port 8030 --reload
