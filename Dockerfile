FROM python:3.12-slim
RUN useradd --create-home pgfederate
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
USER pgfederate
# Extensions are fetched at build time, so the running container needs no download.
RUN python -c "import duckdb; c = duckdb.connect(); [c.execute('INSTALL ' + e) for e in ('postgres', 'mysql', 'sqlite')]"
EXPOSE 5433 9187
ENTRYPOINT ["pgfederate"]
CMD ["serve", "-c", "/etc/pgfederate/pgfederate.yaml"]
