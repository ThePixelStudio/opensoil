FROM python:3.11-slim

WORKDIR /app

# Install runtime deps in a separate layer (cached unless pyproject.toml changes).
COPY pyproject.toml .
RUN pip install --no-cache-dir \
    "pyyaml>=6.0" \
    "paho-mqtt>=1.6" \
    "anthropic>=0.25" \
    "numpy>=1.26" \
    "click>=8.1" \
    "requests>=2.31" \
    "fastapi>=0.109" \
    "uvicorn>=0.27" \
    "aiofiles>=23.0" \
    "python-telegram-bot>=20.0"

# Copy source after deps so the dep layer stays cached on code-only changes.
COPY core/    core/
COPY cli/     cli/
COPY plugins/ plugins/

# Make core / cli / plugins importable without pip install.
ENV PYTHONPATH=/app

# Create the opensoil CLI entrypoint to match pyproject.toml [project.scripts].
RUN printf '#!/usr/bin/env python3\nfrom cli.main import main\nmain()\n' \
    > /usr/local/bin/opensoil && chmod +x /usr/local/bin/opensoil

RUN mkdir -p /root/.opensoil/history /root/.opensoil/logs

EXPOSE 7070
VOLUME ["/root/.opensoil"]

ENTRYPOINT ["opensoil", "--config", "/app/config.yaml"]
CMD ["start"]
