# ADK Python Docs Review Notes

Reviewed date: 2026-03-14

Primary page reviewed:
- https://google.github.io/adk-docs/get-started/python/

Related docs reviewed from that page:
- Command line: https://google.github.io/adk-docs/runtime/command-line/
- Web interface: https://google.github.io/adk-docs/runtime/web/
- Models and Authentication: https://google.github.io/adk-docs/agents/models/
- Gemini model/auth details: https://google.github.io/adk-docs/agents/models/google-cloud/
- ADK CLI reference: https://google.github.io/adk-docs/tools/cli/

## What The Getting-Started Flow Requires
- Install ADK Python package.
- Create an app with `adk create <app_name>`.
- Implement/update `agent.py` (and optional tools).
- Run locally with:
  - `adk run <app_name>` (CLI)
  - `adk web` (Web UI)

## Runtime Notes
- `adk run` is interactive and sends user messages to the configured model.
- `adk web` is a local dev server for debugging and testing agents.
- The web server can be configured with `--host` and `--port`.

## Auth and Model Notes
- AI Studio mode:
  - `GOOGLE_GENAI_USE_VERTEXAI=0`
  - `GOOGLE_API_KEY=<key>`
- Vertex AI mode:
  - `GOOGLE_GENAI_USE_VERTEXAI=1`
  - `GOOGLE_CLOUD_PROJECT=<project>`
  - `GOOGLE_CLOUD_LOCATION=<location>`
- Model strings can be set directly in `Agent(model="...")`.

## Changes Applied To This Repo
- Installed ADK in `.venv`.
- Created `cloud_tutor_agent/` with `adk create`.
- Updated `cloud_tutor_agent/agent.py` to include a real callable tool (`get_current_time`).
- Updated `cloud_tutor_agent/.env` and added `.env.example`.
- Added repeatable verification script: `scripts/verify_adk_setup.sh`.

## Local Verification Performed
- `adk --version` succeeds.
- `python` import of `cloud_tutor_agent.agent` succeeds.
- `adk web --host 127.0.0.1 --port <port>` starts and serves HTML over HTTP.
- `adk run cloud_tutor_agent` starts; model call fails until real credentials are set (expected).

## Action Required Before Real Inference
- Replace placeholder credentials in `cloud_tutor_agent/.env` with valid AI Studio or Vertex AI config.
