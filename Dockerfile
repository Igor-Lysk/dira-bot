FROM python:3.12-slim

WORKDIR /app

# Зависимости отдельным слоем: код меняется часто, список пакетов — редко.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# База лежит на томе, а не в образе: пересборка не должна стирать собранное.
ENV DB_PATH=/app/data/dira.db
VOLUME ["/app/data"]

CMD ["python", "-u", "main.py"]
