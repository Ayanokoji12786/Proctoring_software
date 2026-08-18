# Build context is the exam-proctoring/ repo root (see ../docker-compose.yml),
# so this can copy both server/ and dashboard/ and preserve the same relative
# layout ("../dashboard" next to "server/") the app expects at runtime.
#
# Only the server is containerized. The Student Agent is a desktop app that
# needs direct OS-level access to the local camera, active window, and
# display configuration on the student's own machine — containerizing it
# would defeat its purpose, so it always runs natively (see README).
FROM python:3.12-slim

WORKDIR /app

COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

COPY server/ server/
COPY dashboard/ dashboard/

WORKDIR /app/server
RUN mkdir -p /app/server/data /app/server/evidence_storage

EXPOSE 8000

# Shell form so ${PORT} expands: Railway/Render/Fly-style platforms inject a
# PORT env var and route traffic to whatever port the app actually binds;
# falls back to 8000 for plain `docker compose up` where PORT isn't set.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
