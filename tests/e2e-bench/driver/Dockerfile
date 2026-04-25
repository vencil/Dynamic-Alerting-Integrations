# Driver container — runs INSIDE the docker-compose stack (per design
# §2.4) so all timestamps share one kernel clock with the services.
#
# Stdlib-only Python; no pip install needed. Alpine for minimal size.

FROM python:3.13-alpine
WORKDIR /app
COPY driver.py /app/driver.py
ENTRYPOINT ["python3", "/app/driver.py"]
