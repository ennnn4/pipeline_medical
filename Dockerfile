# Render 배포용 이미지.
# 핵심: poppler-utils(=pdftotext)를 깔아서 PDF 자료도 텍스트 추출되게 함.
# (zip·docx·pptx·txt는 파이썬 표준 기능이라 별도 설치 불필요)
FROM python:3.12-slim

# pdftotext(PDF), 필요한 로케일. hwp는 선택이라 미포함.
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUTF8=1 \
    PYTHONUNBUFFERED=1

# Render가 $PORT를 주입. 파이프라인은 백그라운드 스레드로 돌아 요청은 즉시 응답 →
# threads로 폴링(/status)과 동시 처리, timeout은 넉넉히.
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 600
