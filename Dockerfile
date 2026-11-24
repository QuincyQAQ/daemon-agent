FROM python:3.12-slim AS core
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
RUN useradd --create-home --uid 10001 foldjay && mkdir -p /workspace && chown foldjay:foldjay /workspace
WORKDIR /app
COPY foldjay /app/foldjay
COPY examples /app/examples
COPY tests /app/tests
USER foldjay
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "foldjay.main"]
CMD ["run"]

FROM core AS live
USER root
RUN pip install --no-cache-dir 'openai>=1.30,<3' 'anthropic>=0.40,<1'
USER foldjay
