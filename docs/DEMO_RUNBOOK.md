# CloudTutor Demo Runbook

## Goal
Deliver a clean 3-4 minute walkthrough that proves:
- realtime voice interaction,
- grounded cloud answers with sources,
- doc-navigation deep dive with narrated actions,
- tutorial artifact generation with diagram,
- cloud deployment evidence.

## Pre-Demo Checklist
1. Local/Cloud health:
   - `curl <BACKEND_URL>/health`
   - `curl <BACKEND_URL>/computer-use/health`
2. Credentials and access:
   - `make verify-cloud`
3. Frontend readiness:
   - `make verify-next`
4. Session verifiers:
   - `make verify-session07`
   - `make verify-session08`
   - `make verify-session11`
5. Browser readiness:
   - Playwright Chromium installed for local fallback.

## Suggested Demo Timeline
1. 00:00-00:30: Intro + architecture framing.
2. 00:30-01:20: Ask voice question: "What is Google Cloud Functions?"
3. 01:20-01:50: Barge-in mid-response to show interruption handling.
4. 01:50-02:30: Say "show me more", confirm `yes`, observe narrated doc navigation.
5. 02:30-03:10: Generate tutorial artifact and open HTML.
6. 03:10-03:40: Show Cloud Run URL + logs + health endpoints.

## Backup Plan
- If Computer Use fails, demonstrate grounded fallback path with citations.
- If voice input is noisy, switch to text while keeping live responses enabled.
- If cloud quota is exhausted, run local verification scripts and show stored artifacts.

## Evidence to Capture
- Web app with live captions.
- Citation links in responses.
- Computer Use run result and optional debug URL.
- Generated artifact page with Mermaid diagram.
- Cloud Run service URL and logs.
