# CloudTutor Rubric Mapping

## Innovation
- Realtime multimodal tutor (voice in/out + web navigation + artifact output).
- Human-in-the-loop safety flow for sensitive browser actions.
- Interrupt-and-resume conversational pattern during live response playback.

## Technical Execution
- ADK live streaming architecture with `LiveRequestQueue` and `run_live`.
- Grounded factual responses with `google_search` citations.
- Computer Use worker with provider flexibility (Playwright default, Browserbase optional).
- Durable persistence and artifact output paths with optional Firestore/GCS mirrors.
- Cloud Run deploy + Terraform assets for reproducible infrastructure.

## UX and Demo Quality
- Guided Next.js frontend flow (connect -> ask -> deep dive -> artifact).
- Clear runtime status indicators for connection/mic/live activity.
- Downloadable tutorial artifact with summary, steps, and Mermaid diagram.
- Demo runbook and verification scripts for predictable judge walkthrough.

## Production Readiness Signals
- Request-id middleware and access logging.
- Firestore mirror graceful degradation on repeated failures.
- Dedicated Cloud Run runtime service account via deploy script defaults.
- Session verification scripts covering core flows through deployment checks.
