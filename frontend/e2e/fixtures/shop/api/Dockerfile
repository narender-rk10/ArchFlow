FROM python:3.12-slim
COPY . /app
RUN pip install -r /app/requirements.txt
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--app-dir", "/app"]
