# Firebase Cloud Functions Demo Script (3-4 Minutes)

## Deployed Backend (Use This in Recording)
- Primary URL: `https://cloudtutor-backend-77xovxo4ia-uc.a.run.app`
- Alternate URL: `https://cloudtutor-backend-835452652901.us-central1.run.app`

## Demo Goal
Show a live, interruptible voice tutor that can:
- answer a cloud question,
- handle barge-in naturally,
- launch browser walkthrough with confirmation,
- narrate documentation progress,
- and generate a reusable tutorial artifact.

## Pre-Flight (Before Recording)
1. `make dev-next`
2. Open `http://127.0.0.1:4174`
3. In Session Settings, set **Backend URL** to:
   - `https://cloudtutor-backend-77xovxo4ia-uc.a.run.app`
4. Confirm UI shows connected backend and navigator health ready.
5. Use a fresh Session ID (for clean transcript).

## Recording Timeline

### 00:00-00:20 Intro (you speak)
"Hi everyone, this is CloudTutor. The problem we are solving is that reading cloud documentation is static, lonely, and frankly overwhelming.

Our solution is a voice-first, interactive learning platform powered by the Gemini Live API and the Google Agent Development Kit. It goes beyond a simple text box by actually taking over a browser to walk you through documentation visually, completely redefining self-serve education."

### 00:20-00:55 Ask + concise answer
User says:
"What is Firebase Cloud Functions?"

Expected behavior:
- Agent gives a concise definition.
- Agent offers deep dive in browser (show-me-more style prompt).

### 00:55-01:15 Interrupt (barge-in)
While the agent is still speaking, user interrupts:
"Wait, what does serverless mean here?"

Expected behavior:
- Playback stops quickly.
- Agent answers interruption directly.
- Conversation continues naturally.

### 01:15-01:45 Trigger walkthrough
User says:
"Show me more."

If confirmation card appears:
- Say: "Yes, go for it." (or click `Approve Launch` once)

Expected behavior:
- Left panel shows launch/search timeline events.
- Split screen appears.
- Right panel loads live browser workspace.

### 01:45-02:35 Guided docs teaching
User says:
"Continue."

Expected behavior:
- Agent narrates what is happening while navigation runs.
- Agent explains one use case at a time.
- Agent asks if you want to continue to next use case.

User follow-up:
"Give me a practical example."

Expected behavior:
- Agent answers in lecture style, then offers next step.

### 02:35-03:05 Next use case control
User says:
"Go to the next use case."

Expected behavior:
- Navigator advances and timeline updates.
- Agent pauses for questions after explanation.

### 03:05-03:35 Artifact generation
User says:
"Generate the tutorial artifact for this session."

Expected behavior:
- Artifact appears with summary + steps + diagram.
- Open/download HTML (and PDF if available).

### 03:35-04:00 Cloud proof (submission requirement)
Show:
1. Cloud Run URL for backend.
2. Health endpoint response.
3. Short log snippet proving live traffic.

Suggested commands (in terminal):
- `gcloud run services describe cloudtutor-backend --project cloudtutor-490215 --region us-central1 --format='value(status.url,status.latestReadyRevisionName)'`
- `curl https://cloudtutor-backend-77xovxo4ia-uc.a.run.app/health`
- `curl https://cloudtutor-backend-77xovxo4ia-uc.a.run.app/computer-use/health`
- `gcloud run services logs read cloudtutor-backend --project cloudtutor-490215 --region us-central1 --limit 30`

## Voice Prompts You Can Reuse
- "What is Firebase Cloud Functions?"
- "Show me more."
- "Yes, go for it."
- "Wait, what does serverless mean?"
- "Continue."
- "Go to the next use case."
- "Generate the tutorial artifact."

## Fast Recovery Lines (if anything drifts)
- If browser launch stalls: "Approve launch and continue." (or click `Approve Launch` once)
- If narration pauses: "Continue the walkthrough."
- If topic drifts: "Focus on Firebase Cloud Functions official docs."
- If audio is noisy: switch to text input for one turn, then resume voice.

## What Judges Should Clearly See
- Live interruption working.
- Voice-led confirmation and browser launch.
- Split-screen docs walkthrough.
- Timeline/log progress in left panel.
- Real teaching loop: explain -> ask -> continue.
- Generated artifact and cloud deployment proof.
