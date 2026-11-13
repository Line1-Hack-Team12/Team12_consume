# AI 서비스 (FastAPI). docker compose 의 `ai` 서비스로 띄우고 Spring 만 http://ai:8001 로 부른다.
#
#   빌드:  docker build -t team12-ai ./backend
#   실행:  docker run --rm --env-file backend/.env -p 127.0.0.1:8001:8001 team12-ai
#
# 이미지에 들어가는 것: requirements.txt 와 서비스 코드(.py) 뿐. .env, samples/, truth/, 결과 json 은 .dockerignore 로 막고
# 아래 COPY 도 파일을 하나씩 지정해서 실수로 끌려 들어올 길을 없앤다. API 키는 env_file 로만 주입한다.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 의존성 먼저 (코드가 바뀌어도 이 레이어는 캐시됨)
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 서비스 코드만. 테스트·평가 스크립트·정답 세트는 넣지 않는다
COPY service.py llm_common.py f03_extraction.py bank_file_parser.py classify.py narrate.py notify.py ./

# 루트로 돌리지 않는다. 임시 업로드는 /tmp 에만 쓴다
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8001

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8001/health || exit 1

# 워커 1개면 충분하다: 요청 처리는 async 이고 추출은 5장 동시, 분류는 3묶음 동시로 서비스 안에서 제한한다
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1", "--timeout-keep-alive", "130"]
