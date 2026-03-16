"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { motion, AnimatePresence } from "framer-motion";

type ConnectionState = "disconnected" | "connecting" | "connected" | "error";
type MicState = "stopped" | "active";
type ComputerUseProvider = "playwright" | "browserbase";

type ComputerUseHealthResponse = {
  status: "ready" | "degraded";
  provider_default: ComputerUseProvider;
  providers: Record<string, { ready?: boolean }>;
  model_default: string;
  active_runs: number;
  notes: string[];
};

type TutorialArtifactResponse = {
  artifact_id: string;
  created_at: string;
  topic: string;
  summary: string;
  key_points: string[];
  tutorial_steps: string[];
  check_for_understanding: string;
  mermaid_diagram: string;
  citations: Citation[];
  html_path: string;
  html_url: string;
  pdf_path?: string | null;
  pdf_url?: string | null;
  notes: string[];
};

type AudioPart = {
  type?: string;
  data?: string;
  mime_type?: string;
  stream?: "binary" | "base64";
  byte_length?: number;
};

type Citation = {
  title: string;
  url: string;
};

type LiveMessage = {
  type?: string;
  author?: string;
  is_partial?: boolean;
  reason?: string;
  flow_metadata?: Record<string, unknown>;
  interrupted?: boolean;
  turn_complete?: boolean;
  input_transcription?: { text?: string; is_final?: boolean };
  output_transcription?: { text?: string; is_final?: boolean };
  parts?: AudioPart[];
  citations?: Citation[];
  [key: string]: unknown;
};

type DocNavigatorSafety = {
  runId: string;
  confirmationId: string;
  stepIndex: number;
  action: string;
  explanation?: string;
  args: Record<string, unknown>;
};

type DocNavigatorStep = {
  index: number;
  action: string;
  status: string;
  url?: string | null;
  error?: string | null;
};

type AudioCaptureState = {
  stream: MediaStream | null;
  context: AudioContext | null;
  source: MediaStreamAudioSourceNode | null;
  processor: ScriptProcessorNode | null;
  workletNode: AudioWorkletNode | null;
  silenceGain: GainNode | null;
  analyser: AnalyserNode | null;
};

type AudioPlaybackState = {
  context: AudioContext | null;
  nextStartTime: number;
  pendingBinaryMetadata: Array<{ mimeType: string }>;
  activeSources: AudioBufferSourceNode[];
  analyser: AnalyserNode | null;
  masterGain: GainNode | null;
};

function initialAudioCaptureState(): AudioCaptureState {
  return {
    stream: null,
    context: null,
    source: null,
    processor: null,
    workletNode: null,
    silenceGain: null,
    analyser: null,
  };
}

function initialAudioPlaybackState(): AudioPlaybackState {
  return {
    context: null,
    nextStartTime: 0,
    pendingBinaryMetadata: [],
    activeSources: [],
    analyser: null,
    masterGain: null,
  };
}

function floatTo16BitPCM(float32Array: Float32Array): Int16Array {
  const pcm = new Int16Array(float32Array.length);
  for (let i = 0; i < float32Array.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, float32Array[i] ?? 0));
    pcm[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return pcm;
}

function pcm16ToLittleEndianBytes(pcm16: Int16Array): Uint8Array {
  const buffer = new ArrayBuffer(pcm16.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < pcm16.length; i += 1) {
    view.setInt16(i * 2, pcm16[i] ?? 0, true);
  }
  return new Uint8Array(buffer);
}

function downsampleBuffer(
  buffer: Float32Array,
  inputSampleRate: number,
  outputSampleRate: number,
): Float32Array {
  if (outputSampleRate >= inputSampleRate) {
    return buffer;
  }

  const ratio = inputSampleRate / outputSampleRate;
  const newLength = Math.round(buffer.length / ratio);
  const result = new Float32Array(newLength);
  let offsetResult = 0;
  let offsetBuffer = 0;

  while (offsetResult < result.length) {
    const nextOffsetBuffer = Math.round((offsetResult + 1) * ratio);
    let accum = 0;
    let count = 0;

    for (let i = offsetBuffer; i < nextOffsetBuffer && i < buffer.length; i += 1) {
      accum += buffer[i] ?? 0;
      count += 1;
    }

    result[offsetResult] = count > 0 ? accum / count : 0;
    offsetResult += 1;
    offsetBuffer = nextOffsetBuffer;
  }

  return result;
}

function base64ToBytes(base64: string): Uint8Array {
  const binary = window.atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

function parseSampleRate(mimeType: string | undefined): number {
  const match = /rate=(\d+)/i.exec(mimeType ?? "");
  if (!match) {
    return 24000;
  }
  const parsed = Number.parseInt(match[1] ?? "", 10);
  return Number.isFinite(parsed) ? parsed : 24000;
}

function buildWsUrl(httpUrl: string, userId: string, sessionId: string): string {
  const base = new URL(httpUrl);
  const protocol = base.protocol === "https:" ? "wss:" : "ws:";
  const localTime = new Date().toLocaleTimeString();
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return `${protocol}//${base.host}/ws/${encodeURIComponent(userId)}/${encodeURIComponent(sessionId)}?local_time=${encodeURIComponent(localTime)}&tz=${encodeURIComponent(tz)}`;
}

function formatPayload(payload: unknown): string {
  try {
    return JSON.stringify(payload, null, 2);
  } catch {
    return String(payload);
  }
}

function normalizeCitations(input: unknown): Citation[] {
  if (!Array.isArray(input)) {
    return [];
  }
  const results: Citation[] = [];
  for (const item of input) {
    if (item && typeof item === "object") {
      const title = typeof item.title === "string" ? item.title : "";
      const url = typeof item.url === "string" ? item.url : "";
      if (url) {
        results.push({ title: title || url, url });
      }
    }
  }
  return results;
}

const REASON_TO_STAGE: Record<string, string> = {
  doc_confirmation_required: "awaiting_confirmation",
  confirmation_unclear: "awaiting_confirmation",
  doc_navigation_launching: "launching",
  doc_navigation_searching: "launching",
  doc_navigation_progress: "navigating",
  doc_navigation_already_running: "navigating",
  doc_navigation_resumed: "resumed",
  doc_navigation_pause_requested: "pause_requested",
  doc_navigation_interrupted: "interrupted",
  doc_navigation_paused: "paused",
  doc_navigation_safety_pause: "awaiting_safety",
  doc_navigation_safety_confirmation_unclear: "awaiting_safety",
  doc_navigation_safety_approved: "safety_approved",
  doc_navigation_safety_denied: "paused",
  doc_navigation_safety_missing: "paused",
  doc_navigation_safety_response_error: "awaiting_safety",
  doc_navigation_completed: "completed",
  doc_navigation_locating_use_cases: "locating_use_cases",
  doc_navigation_failed_fallback: "failed",
  doc_navigation_exception_fallback: "failed",
  doc_navigation_failed_status_fallback: "failed",
  doc_use_case_ready: "use_case_ready",
  doc_use_case_teaching: "teaching_use_case",
  doc_use_case_awaiting_followup: "awaiting_user_followup",
  doc_use_case_advancing: "teaching_use_case",
  doc_use_case_stopped: "paused",
};

const REASON_TO_EVENT_TEXT: Record<string, string> = {
  doc_confirmation_required: "Waiting for launch confirmation (yes/no).",
  doc_navigation_launching: "Launching browser walkthrough.",
  doc_navigation_searching: "Searching official documentation.",
  doc_navigation_progress: "Browser walkthrough progress update.",
  doc_navigation_already_running: "Browser walkthrough is already active.",
  doc_navigation_safety_pause: "Paused for safety approval.",
  doc_navigation_safety_confirmation_unclear: "Safety reply unclear. Waiting for yes/no.",
  doc_navigation_safety_approved: "Safety action approved. Continuing.",
  doc_navigation_safety_denied: "Safety action denied. Browser action stopped.",
  doc_navigation_paused: "Browser walkthrough paused.",
  doc_navigation_resumed: "Browser walkthrough resumed.",
  doc_navigation_completed: "Browser walkthrough completed.",
  doc_navigation_locating_use_cases: "Confirming documentation page is open.",
  doc_navigation_failed_fallback: "Browser navigation failed. Using fallback.",
  doc_navigation_exception_fallback: "Browser exception encountered. Using fallback.",
  doc_navigation_failed_status_fallback: "Browser run ended with failure status.",
  doc_use_case_ready: "Documentation open. Ready to explore.",
  doc_use_case_teaching: "Explaining documentation topic.",
  doc_use_case_awaiting_followup: "Waiting for your question.",
  doc_use_case_advancing: "Navigating to the next topic.",
  doc_use_case_stopped: "Documentation walkthrough paused.",
};

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : null;
}

function extractFlowMetadata(payload: unknown): Record<string, unknown> | null {
  const event = asRecord(payload);
  if (!event) return null;
  return asRecord(event.flow_metadata);
}

function extractNavigatorRecord(payload: unknown): Record<string, unknown> | null {
  const metadata = extractFlowMetadata(payload);
  if (!metadata) return null;
  const nested = asRecord(metadata.navigator);
  return nested ?? metadata;
}

function extractNavigatorSessionInfo(payload: unknown): {
  session_url: string;
  current_url: string;
} | null {
  const nav = extractNavigatorRecord(payload);
  if (!nav) return null;

  const activeSession = asRecord(nav.active_session);
  const session_url = typeof activeSession?.debug_url === "string"
    ? activeSession.debug_url
    : typeof nav.debug_url === "string"
      ? nav.debug_url
    : typeof nav.session_url === "string"
      ? nav.session_url
      : "";
  const current_url = typeof activeSession?.current_url === "string"
    ? activeSession.current_url
    : typeof nav.current_url === "string"
      ? nav.current_url
      : typeof nav.visited_url === "string"
        ? nav.visited_url
        : "";

  if (!session_url && !current_url) {
    return null;
  }

  return { session_url, current_url };
}

function extractNavigatorSteps(payload: unknown): DocNavigatorStep[] {
  const nav = extractNavigatorRecord(payload);
  if (!nav) return [];

  const rawSteps = Array.isArray(nav.active_steps)
    ? nav.active_steps
    : Array.isArray(nav.steps)
      ? nav.steps
      : [];

  if (rawSteps.length === 0) return [];

  return rawSteps
    .map((raw, index) => {
      const step = asRecord(raw);
      if (!step) return null;
      return {
        index: typeof step.index === "number" ? step.index : index + 1,
        action: typeof step.action === "string" ? step.action : "step",
        status: typeof step.status === "string" ? step.status : "unknown",
        url: typeof step.url === "string" ? step.url : null,
        error: typeof step.error === "string" ? step.error : null,
      } as DocNavigatorStep;
    })
    .filter((step): step is DocNavigatorStep => Boolean(step));
}

function extractNavigatorTopic(payload: unknown): string | null {
  const nav = extractNavigatorRecord(payload);
  if (!nav) return null;
  return typeof nav.topic === "string" ? nav.topic : null;
}

function extractNavigatorSafety(payload: unknown): DocNavigatorSafety | null {
  const nav = extractNavigatorRecord(payload);
  if (!nav) return null;

  const pending = asRecord(nav.pending_safety_confirmation) ?? asRecord(nav.pending_confirmation);
  if (!pending) return null;

  return {
    runId: typeof nav.run_id === "string" ? nav.run_id : "",
    confirmationId:
      typeof pending.confirmation_id === "string" ? pending.confirmation_id : "",
    stepIndex: typeof pending.step_index === "number" ? pending.step_index : 0,
    action: typeof pending.action === "string" ? pending.action : "",
    explanation:
      typeof pending.explanation === "string" ? pending.explanation : undefined,
    args: asRecord(pending.args) ?? {},
  };
}

function extractNavigatorStage(payload: unknown): string | null {
  const nav = extractNavigatorRecord(payload);
  if (nav && typeof nav.stage === "string") {
    return nav.stage;
  }

  const event = asRecord(payload);
  if (!event || typeof event.reason !== "string") {
    return null;
  }

  return REASON_TO_STAGE[event.reason] ?? null;
}

function extractNavigatorEvents(payload: unknown): string[] {
  const nav = extractNavigatorRecord(payload);
  if (nav && typeof nav.latest_event === "string" && nav.latest_event.trim()) {
    return [nav.latest_event.trim()];
  }

  const event = asRecord(payload);
  if (!event || typeof event.reason !== "string") {
    return [];
  }

  const mapped = REASON_TO_EVENT_TEXT[event.reason];
  if (mapped) {
    return [mapped];
  }

  const output = asRecord(event.output_transcription);
  const outputText = typeof output?.text === "string" ? output.text.trim() : "";
  if (outputText && event.author === "cloudtutor.flow") {
    return [outputText];
  }

  return [];
}

function resolveBackendPath(baseUrl: string, relativePath: string): string {
  const cleanBase = baseUrl.replace(/\/+$/, "");
  const cleanRel = relativePath.replace(/^\/+/, "");
  return `${cleanBase}/${cleanRel}`;
}

function generateShortId(): string {
  return Math.random().toString(36).substring(2, 8);
}

function detectVoiceControlIntent(text: string): "stop" | "request_more_details" | "none" {
  const normalized = text.toLowerCase().trim().replace(/\s+/g, " ");
  const stripped = normalized.replace(/[.,!?]$/, "").trim();

  const stopPhrases = [
    "stop",
    "halt",
    "pause",
    "wait",
    "hold on",
    "shut up",
    "cancel",
    "enough",
    "that's enough",
    "quiet",
  ];

  const moreDetailsPhrases = [
    "show me more",
    "show me the docs",
    "open the docs",
    "let's see more",
    "show me",
    "yes show me more",
    "yeah show me more",
    "okay show me more",
    "ok show me more",
    "sure show me more",
  ];

  if (stopPhrases.includes(stripped) || (stripped.startsWith("stop") && stripped.length < 12)) {
    return "stop";
  }

  if (
    moreDetailsPhrases.includes(stripped) ||
    /\b(show me more|more details|go deeper|dig deeper|tell me more|open (the )?docs?|open documentation|official docs?)\b/.test(
      stripped,
    )
  ) {
    return "request_more_details";
  }
  return "none";
}

function normalizeControlText(text: string): string {
  return text.toLowerCase().trim().replace(/[.,!?]$/, "");
}

function classifyVoiceConfirmation(text: string): "yes" | "no" | "unknown" {
  const normalized = normalizeControlText(text).replace(/\s+/g, " ").trim();
  const yesValues = new Set([
    "yes",
    "y",
    "ya",
    "ja",
    "yeah",
    "yep",
    "yup",
    "sure thing",
    "sure",
    "ok",
    "okay",
    "go ahead",
    "go for it",
    "please do",
    "continue",
    "yes please",
    "sure yes",
    "affirmative",
    "let's do it",
    "lets do it",
    "sounds good",
    "do it",
  ]);
  const noValues = new Set([
    "no",
    "n",
    "nah",
    "nej",
    "nope",
    "not now",
    "later",
    "cancel",
    "stop",
    "no thanks",
    "don't",
    "do not",
    "negative",
  ]);
  if (yesValues.has(normalized)) return "yes";
  if (noValues.has(normalized)) return "no";
  if (/\b(yes|yeah|yep|yup|ja|affirmative)\b/.test(normalized)) return "yes";
  if (/\b(no|nope|nah|nej|negative)\b/.test(normalized)) return "no";
  return "unknown";
}

function formatNavigatorStageLabel(stage: string): string {
  if (!stage) return "idle";
  return stage
    .replace(/_/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

export default function Home() {
  const [backendUrl, setBackendUrl] = useState<string>("http://localhost:8080");
  const [userId, setUserId] = useState<string>("local-user");
  // Cynical stability fix: Generate a unique session ID per page load so refreshes do NOT resume old buffered audio.
  const [sessionId] = useState<string>(() => `session-${Math.random().toString(36).substring(2, 9)}`);
  const [connectionState, setConnectionState] = useState<ConnectionState>("disconnected");
  const [micState, setMicState] = useState<MicState>("stopped");
  const [micError, setMicError] = useState<string | null>(null);
  const [liveStatus, setLiveStatus] = useState<string>("Ready");
  const [eventLog, setEventLog] = useState<string[]>([]);
  const [userTranscript, setUserTranscript] = useState<string>("");
  const [agentTranscript, setAgentTranscript] = useState<string>("");
  const [messageInput, setMessageInput] = useState<string>("");
  const [latestCitations, setLatestCitations] = useState<Citation[]>([]);
  
  // UI scaling hook for orb
  const [volumeScale, setVolumeScale] = useState<number>(0);

  const [navigatorSessionUrl, setNavigatorSessionUrl] = useState<string | null>(null);
  const [navigatorCurrentUrl, setNavigatorCurrentUrl] = useState<string | null>(null);
  const [navigatorStage, setNavigatorStage] = useState<string>("idle");
  const [navigatorSteps, setNavigatorSteps] = useState<DocNavigatorStep[]>([]);
  const [navigatorSafety, setNavigatorSafety] = useState<DocNavigatorSafety | null>(null);
  const [navigatorTimeline, setNavigatorTimeline] = useState<string[]>([]);
  const [navigatorTopic, setNavigatorTopic] = useState<string>("");
  const [latestAgentFinal, setLatestAgentFinal] = useState<string>("");

  const [isArtifactMode, setIsArtifactMode] = useState<boolean>(false);
  const [artifactResult, setArtifactResult] = useState<TutorialArtifactResponse | null>(null);
  const [artifactBusy, setArtifactBusy] = useState<boolean>(false);
  const [artifactError, setArtifactError] = useState<string>("");
  const [artifactTopic] = useState<string>("");
  const [artifactIncludePdf] = useState<boolean>(false);
  const [citationHistory, setCitationHistory] = useState<Citation[]>([]);

  const [computerUseHealth, setComputerUseHealth] = useState<ComputerUseHealthResponse | null>(null);
  const [computerUseHealthBusy, setComputerUseHealthBusy] = useState<boolean>(false);
  const [computerUseError, setComputerUseError] = useState<string | null>(null);
  const [computerUseProvider, setComputerUseProvider] = useState<ComputerUseProvider>("playwright");

  const socketRef = useRef<WebSocket | null>(null);
  const audioCaptureRef = useRef<AudioCaptureState & { inactivityInterval?: NodeJS.Timeout }>(initialAudioCaptureState());
  const audioPlaybackRef = useRef<AudioPlaybackState>(initialAudioPlaybackState());
  const lastBargeInLogMsRef = useRef<number>(0);
  const lastVoiceControlRef = useRef<{ normalized: string; atMs: number } | null>(null);
  const lastUserAudioMsRef = useRef<number>(Date.now());
  const connectionConfigRef = useRef<Record<string, any>>({});
  const consecutiveSpeechFramesRef = useRef<number>(0);
  const lastClientSpeechSignalMsRef = useRef<number>(0);
  const playbackWasActiveRef = useRef<boolean>(false);
  const playbackBargeEligibleAtMsRef = useRef<number>(0);
  const playbackRmsBufferRef = useRef<Float32Array | null>(null);
  const silenceInputBufferRef = useRef<Float32Array | null>(null);
  const lastNavigatorVoiceDecisionRef = useRef<{ key: string; atMs: number } | null>(null);
  const navigatorStageRef = useRef<string>("idle");
  const lastNavigatorEventRef = useRef<string>("");
  const animationFrameRef = useRef<number>(0);
  const speechFrameCountRef = useRef<number>(0);

  // Auto-connect on page load
  useEffect(() => {
    // Attempt auto-connect with a brief delay so refs are fully mounted
    const autoStartTimer = setTimeout(() => {
        if (connectionState === "disconnected") {
            console.log("[Auto-Connect] Attempting to awaken CloudTutor automatically.");
            connect();
        }
    }, 1000);
    return () => clearTimeout(autoStartTimer);
  }, []);

  // Global touch/click listener to unlock AudioContext autoplay rules for follow-ups
  useEffect(() => {
    const unlockAudio = () => {
      const context = audioPlaybackRef.current.context;
      if (context && context.state === 'suspended') {
        void context.resume();
        console.log("[CloudTutor] AudioContext resumed via user interaction.");
      }
    };
    document.addEventListener("click", unlockAudio, { capture: true });
    document.addEventListener("touchstart", unlockAudio, { capture: true });
    document.addEventListener("keydown", unlockAudio, { capture: true });
    return () => {
      document.removeEventListener("click", unlockAudio, { capture: true });
      document.removeEventListener("touchstart", unlockAudio, { capture: true });
      document.removeEventListener("keydown", unlockAudio, { capture: true });
    };
  }, []);

  // Sync ref so voice logic has the latest value.
  const startTimestampMs = Date.now();

  useEffect(() => {
    navigatorStageRef.current = navigatorStage;
  }, [navigatorStage]);

  // Cynical stability fix: Ensure WebSocket terminates cleanly if the component silently unmounts
  useEffect(() => {
    return () => {
      if (socketRef.current) {
        console.log("[CloudTutor] Cleaning up WebSocket on unmount");
        socketRef.current.close(1000, "Component unmounted");
      }
    };
  }, []);

  function logEvent(type: string, data?: unknown): void {
    const time = new Date().toLocaleTimeString();
    const entry = `[${time}] ${type}${data ? `\n${formatPayload(data)}` : ""}`;
    setEventLog((prev) => [entry, ...prev].slice(0, 80));
    console.log(`[CloudTutor] ${type}`, data || "");
  }

  function appendUserText(text: string): void {
    if (!text.trim()) return;
    setUserTranscript((prev) => `${prev}\nUser: ${text.trim()}`.trim());
  }

  function appendAgentText(text: string): void {
    if (!text.trim()) return;
    setAgentTranscript((prev) => `${prev}\nAgent: ${text.trim()}`.trim());
  }

  function pushNavigatorEvent(eventText: string): void {
    const trimmed = eventText.trim();
    if (!trimmed) return;
    if (lastNavigatorEventRef.current === trimmed) return;
    lastNavigatorEventRef.current = trimmed;
    const time = new Date().toLocaleTimeString();
    setNavigatorTimeline((prev) => [`[${time}] ${trimmed}`, ...prev].slice(0, 60));
  }

  function sendJson(payload: Record<string, unknown>): void {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return;
    }
    socket.send(JSON.stringify(payload));
  }

  async function generateTutorialArtifact(): Promise<void> {
    if (!userTranscript || userTranscript.length < 5) {
      setArtifactError("Not enough conversation data to generate a tutorial.");
      return;
    }
    setArtifactBusy(true);
    setArtifactError("");

    try {
      const resp = await fetch(`${backendUrl}/artifacts/tutorial`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: userId,
          session_id: sessionId,
          transcript_text: `${userTranscript}\n${agentTranscript}`,
          topic_override: artifactTopic || navigatorTopic || undefined,
          target_duration_minutes: 5,
          include_pdf: artifactIncludePdf,
          citation_history: citationHistory,
        }),
      });

      if (!resp.ok) {
        throw new Error(`Generation failed: ${resp.status}`);
      }

      const result = await resp.json() as TutorialArtifactResponse;
      setArtifactResult(result);
    } catch (err: any) {
      setArtifactError(err.message || "Failed to generate artifact");
      console.error(err);
    } finally {
      setArtifactBusy(false);
    }
  }

  function resetNavigatorState(): void {
    setNavigatorSessionUrl(null);
    setNavigatorCurrentUrl(null);
    setNavigatorStage("idle");
    setNavigatorSteps([]);
    setNavigatorSafety(null);
    setNavigatorTimeline([]);
    setNavigatorTopic("");
    lastNavigatorEventRef.current = "";
  }

  function sendPresetPrompt(promptText: string): void {
    if (connectionState !== "connected") return;
    stopPlaybackNow("preset_prompt");
    sendJson({ mime_type: "text/plain", data: promptText });
    appendUserText(promptText);
    logEvent("preset_prompt_sent", { prompt: promptText });
  }

  function submitNavigatorDecision(
    acknowledged: boolean,
    context: "launch" | "safety",
  ): void {
    if (connectionState !== "connected") {
      logEvent("navigator_decision_ignored", {
        reason: "socket_not_connected",
        context,
        acknowledged,
      });
      return;
    }

    const spokenReply = acknowledged ? "yes" : "no";
    stopPlaybackNow(`navigator_${context}_decision`);
    sendJson({ mime_type: "text/plain", data: spokenReply });

    if (context === "launch") {
      pushNavigatorEvent(
        acknowledged
          ? "Launch confirmed. Preparing browser workspace."
          : "Launch declined. Staying in voice mode.",
      );
      if (acknowledged) {
        setNavigatorStage((current) =>
          current === "awaiting_confirmation" ? "launching" : current,
        );
      } else {
        resetNavigatorState();
      }
    } else {
      pushNavigatorEvent(
        acknowledged
          ? "Safety action approved by user."
          : "Safety action denied by user.",
      );
      setNavigatorSafety(null);
      setNavigatorStage(acknowledged ? "safety_approved" : "paused");
    }
  }

  function routeVoiceControlToBackend(transcript: string): void {
    const intent = detectVoiceControlIntent(transcript);
    if (intent === "none") {
      return;
    }

    const normalized = normalizeControlText(transcript);
    const now = Date.now();
    const last = lastVoiceControlRef.current;
    if (last && last.normalized === normalized && now - last.atMs < 2500) {
      return;
    }

    const stage = navigatorStageRef.current;
    const controlStageAllowed =
      stage === "awaiting_confirmation" ||
      stage === "launching" ||
      stage === "navigating" ||
      stage === "awaiting_safety" ||
      stage === "paused" ||
      stage === "pause_requested" ||
      stage === "interrupted" ||
      stage === "resumed" ||
      stage === "safety_approved" ||
      stage === "failed" ||
      stage === "completed";

    if (intent !== "request_more_details" && !controlStageAllowed) {
      return;
    }

    if (connectionState !== "connected") {
      return;
    }

    lastVoiceControlRef.current = { normalized, atMs: now };
    stopPlaybackNow("voice_control_detected");
    sendJson({ mime_type: "text/plain", data: transcript.trim() });
    logEvent("voice_control_dispatch", {
      transcript: transcript.trim(),
      normalized,
      intent,
      stage,
    });

    if (intent === "request_more_details") {
      setLiveStatus("Awaiting yes/no");
      pushNavigatorEvent("Detected voice deep-dive request and routed to guided flow.");
    } else {
      setLiveStatus("Routing control");
    }
  }

  function ensurePlaybackContext(): AudioContext {
    const playback = audioPlaybackRef.current;
    if (!playback.context) {
      playback.context = new AudioContext();
      playback.nextStartTime = playback.context.currentTime;
      
      const analyser = playback.context.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.5;
      playback.analyser = analyser;
      
      const masterGain = playback.context.createGain();
      playback.masterGain = masterGain;

      masterGain.connect(analyser);
      analyser.connect(playback.context.destination);
    }
    if (playback.context.state === "suspended") {
      void playback.context.resume();
    }
    return playback.context;
  }

  function stopPlaybackNow(reason?: string): void {
    const playback = audioPlaybackRef.current;
    playback.pendingBinaryMetadata = [];

    for (const source of playback.activeSources) {
      try {
        source.stop(0);
      } catch {
        // Ignore stop errors when source already ended.
      }
      try {
        source.disconnect();
      } catch {
        // Ignore disconnect errors for already disposed nodes.
      }
    }
    playback.activeSources = [];

    if (playback.context) {
      playback.nextStartTime = playback.context.currentTime + 0.01;
    }

    if (reason) {
      const now = Date.now();
      if (now - lastBargeInLogMsRef.current > 700) {
        lastBargeInLogMsRef.current = now;
        logEvent("barge_in", { reason });
      }
    }
  }

  function rmsLevel(samples: Float32Array): number {
    if (samples.length === 0) {
      return 0;
    }
    let total = 0;
    for (let i = 0; i < samples.length; i += 1) {
      const sample = samples[i] ?? 0;
      total += sample * sample;
    }
    return Math.sqrt(total / samples.length);
  }

  function playbackRmsLevel(): number {
    const analyser = audioPlaybackRef.current.analyser;
    if (!analyser) {
      return 0;
    }

    const sampleWindow = analyser.fftSize || 512;
    if (!playbackRmsBufferRef.current || playbackRmsBufferRef.current.length !== sampleWindow) {
      playbackRmsBufferRef.current = new Float32Array(sampleWindow);
    }

    const buffer = playbackRmsBufferRef.current as Float32Array<ArrayBuffer>;
    analyser.getFloatTimeDomainData(buffer);
    return rmsLevel(buffer);
  }

  function playPcmBytes(bytes: Uint8Array, mimeType?: string): void {
    const sampleRate = parseSampleRate(mimeType);
    const sampleCount = Math.floor(bytes.length / 2);

    if (sampleCount <= 0) {
      return;
    }

    const pcm16 = new Int16Array(sampleCount);
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);

    for (let i = 0; i < sampleCount; i += 1) {
      pcm16[i] = view.getInt16(i * 2, true);
    }

    const float32 = new Float32Array(sampleCount);
    for (let i = 0; i < sampleCount; i += 1) {
      float32[i] = pcm16[i] / 32768;
    }

    const context = ensurePlaybackContext();
    const audioBuffer = context.createBuffer(1, float32.length, sampleRate);
    audioBuffer.copyToChannel(float32, 0);

    const source = context.createBufferSource();
    source.buffer = audioBuffer;
    
    // Route to master gain which routes to analyser and destination
    const playback = audioPlaybackRef.current;
    if (playback.masterGain) {
      source.connect(playback.masterGain);
    } else {
      source.connect(context.destination);
    }

    const startAt = Math.max(context.currentTime + 0.02, playback.nextStartTime);
    source.start(startAt);
    playback.nextStartTime = startAt + audioBuffer.duration;
    playback.activeSources.push(source);
    source.onended = () => {
      playback.activeSources = playback.activeSources.filter((node) => node !== source);
    };
  }

  async function handleBinarySocketFrame(data: ArrayBuffer | Blob): Promise<void> {
    let bytes: Uint8Array;
    if (data instanceof ArrayBuffer) {
      bytes = new Uint8Array(data);
    } else {
      bytes = new Uint8Array(await data.arrayBuffer());
    }

    const metadata =
      audioPlaybackRef.current.pendingBinaryMetadata.shift() ?? {
        mimeType: "audio/pcm;rate=24000",
      };

    playPcmBytes(bytes, metadata.mimeType);
  }

  function isMicActive(): boolean {
    const capture = audioCaptureRef.current;
    return Boolean(capture.processor || capture.workletNode);
  }

  async function startMicrophone(): Promise<void> {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN || isMicActive()) {
      return;
    }

    try {
      setMicError(null);
      // Cynical stability fix: explicitly demand the OS provide echo cancellation to prevent
      // the agent's voice bouncing off the user's walls from triggering interruptions.
      const stream = await navigator.mediaDevices.getUserMedia({ 
        audio: { 
          echoCancellation: true, 
          noiseSuppression: true,
          autoGainControl: true
        } 
      });
      const context = new AudioContext();
      const source = context.createMediaStreamSource(stream);

      let processor: ScriptProcessorNode | null = null;
      let workletNode: AudioWorkletNode | null = null;
      let silenceGain: GainNode | null = null;
      
      const analyser = context.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.5;

      const sendFloatSamples = (input: Float32Array): void => {
        const liveSocket = socketRef.current;
        if (!liveSocket || liveSocket.readyState !== WebSocket.OPEN) {
          return;
        }

        const now = Date.now();
        const inputRms = rmsLevel(input);
        const playbackRms = playbackRmsLevel();
        const playbackActive =
          audioPlaybackRef.current.activeSources.length > 0 || playbackRms >= 0.012;

        // Prime a short guard window when playback starts to avoid immediate
        // false barge-ins from device echo during TTS ramp-up.
        if (playbackActive) {
          if (!playbackWasActiveRef.current) {
            playbackWasActiveRef.current = true;
            playbackBargeEligibleAtMsRef.current = now + 350;
            consecutiveSpeechFramesRef.current = 0;
          }
        } else if (playbackWasActiveRef.current) {
          playbackWasActiveRef.current = false;
          playbackBargeEligibleAtMsRef.current = 0;
          consecutiveSpeechFramesRef.current = 0;
        }

        const playbackBargeEligible =
          !playbackActive || now >= playbackBargeEligibleAtMsRef.current;
        const humanSpeechThreshold = playbackActive
          ? Math.max(0.07, playbackRms * 1.9, playbackRms + 0.03)
          : 0.045;
        const likelyHumanSpeech =
          inputRms >= humanSpeechThreshold &&
          (!playbackActive || playbackBargeEligible);

        // Track the last time actual human voice was heard for inactivity timeout.
        // While the agent is speaking, require mic energy to exceed playback energy
        // so speaker bleed does not falsely trigger barge-in.
        if (likelyHumanSpeech) {
          lastUserAudioMsRef.current = now;
          consecutiveSpeechFramesRef.current += 1;
        } else {
          consecutiveSpeechFramesRef.current = 0;
        }

        // Emit an explicit interruption signal if sustained loudness is detected.
        const requiredSpeechFrames = playbackActive ? 9 : 5;
        if (
          playbackActive &&
          playbackBargeEligible &&
          consecutiveSpeechFramesRef.current === requiredSpeechFrames
        ) {
          if (now - lastClientSpeechSignalMsRef.current < 1200) {
            return;
          }
          lastClientSpeechSignalMsRef.current = now;

          // Immediate local barge-in: stop current playback right away so
          // interruption feels human-like, then notify backend.
          stopPlaybackNow("local_speech_detected");
          logEvent("client_barge_in_detected", {
            rms: inputRms,
            playbackRms,
            threshold: humanSpeechThreshold,
            requiredSpeechFrames,
          });
          const payload = JSON.stringify({ type: "client_speech_detected" });
          liveSocket.send(payload);
        }

        let micSamples = input;
        if (playbackActive && !likelyHumanSpeech) {
          if (!silenceInputBufferRef.current || silenceInputBufferRef.current.length !== input.length) {
            silenceInputBufferRef.current = new Float32Array(input.length);
          }
          micSamples = silenceInputBufferRef.current;
        }

        const downsampled = downsampleBuffer(micSamples, context.sampleRate, 16000);
        const pcm16 = floatTo16BitPCM(downsampled);
        const littleEndianBytes = pcm16ToLittleEndianBytes(pcm16);
        liveSocket.send(littleEndianBytes.buffer);
      };

      if (context.audioWorklet && typeof AudioWorkletNode !== "undefined") {
        try {
          await context.audioWorklet.addModule("/audio-capture-worklet.js");
          workletNode = new AudioWorkletNode(context, "pcm-capture-processor");
          workletNode.port.onmessage = (event: MessageEvent<unknown>) => {
            if (event.data instanceof Float32Array) {
              sendFloatSamples(event.data);
            }
          };

          silenceGain = context.createGain();
          silenceGain.gain.value = 0;
          source.connect(analyser);
          analyser.connect(workletNode);
          workletNode.connect(silenceGain);
          silenceGain.connect(context.destination);
        } catch {
           logEvent("mic_worklet_fallback", { message: "AudioWorklet unavailable; using ScriptProcessor" });
        }
      }

      if (!workletNode) {
        processor = context.createScriptProcessor(1024, 1, 1);
        processor.onaudioprocess = (event: AudioProcessingEvent) => {
          sendFloatSamples(event.inputBuffer.getChannelData(0));
        };

        silenceGain = context.createGain();
        silenceGain.gain.value = 0;
        source.connect(analyser);
        analyser.connect(processor);
        processor.connect(silenceGain);
        silenceGain.connect(context.destination);
      }

      audioCaptureRef.current = {
        stream,
        context,
        source,
        processor,
        workletNode,
        silenceGain,
        analyser
      };

      // Cynical stability fix: 3 minute deep-idle timeout.
      // If the user walks away taking off their headset, we don't want to keep streaming
      // 16kHz silence to the cloud forever.
      lastUserAudioMsRef.current = Date.now(); // Reset on fresh start
      audioCaptureRef.current.inactivityInterval = setInterval(() => {
        if (Date.now() - lastUserAudioMsRef.current > 180000) {
          console.log("[CloudTutor] 3-Minute Idle Timeout Reached. Tearing down session.");
          logEvent("idle_timeout", { message: "No audio input detected for 3 minutes" });
          if (socketRef.current) {
            socketRef.current.close(1000, "Idle Timeout");
          }
        }
      }, 5000) as unknown as NodeJS.Timeout;

      setMicState("active");
      setLiveStatus("Listening");
      logEvent("mic_start", { captureMode: workletNode ? "audio_worklet" : "script_processor" });
    } catch (error: any) {
      logEvent("mic_error", { message: error instanceof Error ? error.message : "Failed to start" });
      
      // Explicitly handle permission denial
      if (error.name === "NotAllowedError" || error.name === "PermissionDeniedError") {
        setMicError("Microphone access was denied. CloudTutor requires mic permissions to listen.");
        setLiveStatus("Microphone Denied");
      } else {
        setMicError("Failed to access microphone. Please check your browser settings.");
      }
      
      // Ensure socket closes cleanly if mic fails to prevent zombie sessions
      if (socketRef.current) {
         socketRef.current.close();
      }
    }
  }

  function stopMicrophone(): void {
    const capture = audioCaptureRef.current;

    capture.processor?.disconnect();
    capture.workletNode?.disconnect();
    capture.silenceGain?.disconnect();
    capture.source?.disconnect();
    capture.analyser?.disconnect();

    if (capture.stream) {
      capture.stream.getTracks().forEach((track) => track.stop());
    }

    if (capture.context) {
      void capture.context.close();
    }
    
    if (capture.inactivityInterval) {
      clearInterval(capture.inactivityInterval);
    }

    audioCaptureRef.current = initialAudioCaptureState();
    audioPlaybackRef.current.pendingBinaryMetadata = [];

    setMicState("stopped");
    setLiveStatus("Ready");
    logEvent("mic_stop", { message: "Microphone stopped" });
  }

  function handleSocketMessage(rawData: string): void {
    try {
      const data = JSON.parse(rawData) as LiveMessage;

      let receivedAudioPart = false;
      let textFromParts = "";

      if (Array.isArray(data.parts)) {
        for (const part of data.parts) {
          if (part.mime_type?.startsWith("audio/pcm")) {
            receivedAudioPart = true;
            if (part.stream === "binary") {
              audioPlaybackRef.current.pendingBinaryMetadata.push({
                mimeType: part.mime_type,
              });
            } else if (part.data) {
              const bytes = base64ToBytes(part.data);
              playPcmBytes(bytes, part.mime_type);
            }
          }
          if (part.type === "text" && part.data) {
            textFromParts += part.data;
          }
        }
      }

      if (data.interrupted || data.type === "interrupted") {
        stopPlaybackNow("agent_interrupted");
        setLiveStatus("Interrupted");
      } else if (data.turn_complete || data.type === "turn_complete") {
        setLiveStatus("Ready");
      } else if (receivedAudioPart) {
        setLiveStatus("Speaking");
      }

      if (Array.isArray(data.citations) && data.citations.length > 0) {
        setLatestCitations(normalizeCitations(data.citations));
      }

      if (data.input_transcription?.is_final && data.input_transcription.text) {
        const spoken = data.input_transcription.text;
        appendUserText(data.input_transcription.text);
        routeVoiceControlToBackend(spoken);

        const confirmation = classifyVoiceConfirmation(spoken);
        if (navigatorStageRef.current === "awaiting_confirmation") {
          if (confirmation === "yes") {
            const key = `launch:${normalizeControlText(spoken)}`;
            const now = Date.now();
            const last = lastNavigatorVoiceDecisionRef.current;
            if (!last || last.key !== key || now - last.atMs > 2500) {
              lastNavigatorVoiceDecisionRef.current = { key, atMs: now };
              submitNavigatorDecision(true, "launch");
            }
          } else if (confirmation === "no") {
            const key = `launch:${normalizeControlText(spoken)}`;
            const now = Date.now();
            const last = lastNavigatorVoiceDecisionRef.current;
            if (!last || last.key !== key || now - last.atMs > 2500) {
              lastNavigatorVoiceDecisionRef.current = { key, atMs: now };
              submitNavigatorDecision(false, "launch");
            }
          }
        } else if (navigatorStageRef.current === "awaiting_safety") {
          if (confirmation === "yes") {
            const key = `safety:${normalizeControlText(spoken)}`;
            const now = Date.now();
            const last = lastNavigatorVoiceDecisionRef.current;
            if (!last || last.key !== key || now - last.atMs > 2500) {
              lastNavigatorVoiceDecisionRef.current = { key, atMs: now };
              submitNavigatorDecision(true, "safety");
            }
          } else if (confirmation === "no") {
            const key = `safety:${normalizeControlText(spoken)}`;
            const now = Date.now();
            const last = lastNavigatorVoiceDecisionRef.current;
            if (!last || last.key !== key || now - last.atMs > 2500) {
              lastNavigatorVoiceDecisionRef.current = { key, atMs: now };
              submitNavigatorDecision(false, "safety");
            }
          }
        }
      }
      if (data.output_transcription?.is_final && data.output_transcription.text) {
        appendAgentText(data.output_transcription.text);
        setLatestAgentFinal(data.output_transcription.text);
      } else if (textFromParts && !data.is_partial) {
        appendAgentText(textFromParts);
        setLatestAgentFinal(textFromParts);
      }

      const info = extractNavigatorSessionInfo(data);
      if (info) {
        if (info.session_url) {
          setNavigatorSessionUrl(info.session_url);
        }
        if (info.current_url) {
          setNavigatorCurrentUrl(info.current_url);
        }
      }

      const topic = extractNavigatorTopic(data);
      if (topic) {
        setNavigatorTopic(topic);
      }

      const stage = extractNavigatorStage(data);
      if (stage && stage !== navigatorStageRef.current) {
        setNavigatorStage(stage);
      }

      const extractedSteps = extractNavigatorSteps(data);
      if (extractedSteps.length > 0) {
        setNavigatorSteps(extractedSteps);
      }

      const evtList = extractNavigatorEvents(data);
      for (const eMsg of evtList) {
        pushNavigatorEvent(eMsg);
      }

      const safety = extractNavigatorSafety(data);
      if (safety) {
        setNavigatorSafety(safety);
      } else if (stage && stage !== "awaiting_safety") {
        setNavigatorSafety(null);
      }

      if (data.reason && data.author === "cloudtutor.flow") {
        logEvent(`flow_${data.reason}`, data.flow_metadata ?? {});
      } else if (data.type !== "server_content" && data.type !== "turn_complete") {
        logEvent(`socket_${data.type || "unknown"}`, data);
      }
    } catch (err) {
      logEvent("socket_parse_error", { message: err instanceof Error ? err.message : "Parse error", rawData });
    }
  }

  function connect(): void {
    if (connectionState === "connected" || connectionState === "connecting") {
      return;
    }

    setConnectionState("connecting");
    setEventLog([]);
    const wsUrl = buildWsUrl(backendUrl.trim(), userId.trim(), sessionId.trim());

    try {
      const socket = new WebSocket(wsUrl);
      socket.binaryType = "arraybuffer";
      socketRef.current = socket;

      socket.onopen = () => {
        setConnectionState("connected");
        setLiveStatus("Connected");
        logEvent("ws_open", { url: wsUrl });
        
        // Auto-start mic on successful connection
        void startMicrophone();
      };

      socket.onerror = (error) => {
        logEvent("ws_error", error);
        setConnectionState("error");
        setLiveStatus("Connection Error");
        stopMicrophone();
        stopPlaybackNow("ws_error");
      };

      socket.onclose = (event) => {
        logEvent("ws_close", { message: "WebSocket connection closed", code: event.code, reason: event.reason });
        setConnectionState("disconnected");
        setLiveStatus("Disconnected");
        resetNavigatorState();
        stopMicrophone();
        stopPlaybackNow("ws_closed");
      };

      socket.onmessage = async (event: MessageEvent<Blob | string>) => {
        if (typeof event.data === "string") {
          handleSocketMessage(event.data);
        } else if (event.data instanceof ArrayBuffer || event.data instanceof Blob) {
          void handleBinarySocketFrame(event.data);
        }
      };
    } catch (error) {
      setConnectionState("error");
      setLiveStatus("Error");
      logEvent("ws_setup_error", { error });
    }
  }

  function disconnect(): void {
    const socket = socketRef.current;
    if (socket && socket.readyState !== WebSocket.CLOSED) {
      socket.close();
    }
    setConnectionState("disconnected");
    setLiveStatus("Disconnected");
    socketRef.current = null;
    resetNavigatorState();
    stopMicrophone();
    stopPlaybackNow("manual_disconnect");
    
    // Automatically attempt artifact generation if there was meaningful interaction
    if (userTranscript.length > 5) {
      setIsArtifactMode(true);
      void generateTutorialArtifact();
    }
  }

  const splitModeStages = useMemo(
    () =>
      new Set([
        "launching",
        "navigating",
        "locating_use_cases",
        "use_case_ready",
        "teaching_use_case",
        "awaiting_user_followup",
        "resumed",
        "pause_requested",
        "paused",
        "interrupted",
        "awaiting_safety",
        "safety_approved",
        "completed",
        "failed",
      ]),
    [],
  );
  const awaitingLaunchConfirmation = navigatorStage === "awaiting_confirmation";
  const isNavigating = Boolean(navigatorSessionUrl) || splitModeStages.has(navigatorStage);
  const isCompleted = navigatorStage === "completed";
  const stageLabel = formatNavigatorStageLabel(navigatorStage);

  return (
    <div className="app-container">
    
      {/* Microphone Error Overlay */}
      <AnimatePresence>
        {micError && (
          <motion.div 
            className="error-overlay"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          >
            <h2 className="error-title">SYSTEM ALERT</h2>
            <p className="error-text">
              {micError}
              <br/><br/>
              Please click the lock icon in your URL bar and explicitly allow Microphone access to continue the live session.
            </p>
            <button 
              className="start-btn" 
              style={{ position: 'relative', bottom: 'auto' }}
              onClick={() => { setMicError(null); connect(); }}
            >
              RETRY CONNECTION
            </button>
          </motion.div>
        )}
      </AnimatePresence>
      
      {/* Central or Sliding Left Panel */}
      <motion.div
        className={`left-panel ${isNavigating || isArtifactMode ? "left-panel--split" : ""}`}
        animate={{
          width: isNavigating || isArtifactMode ? "38%" : "100%",
          borderRight: isNavigating || isArtifactMode ? "1px solid rgba(255,255,255,0.1)" : "none",
          backgroundColor: isNavigating || isArtifactMode ? "#050505" : "transparent",
        }}
        transition={{ duration: 0.8, ease: "anticipate" }}
      >
        <div
          className="orb-wrapper"
          onClick={() => {
            if (connectionState === "disconnected") connect();
            else if (micState === "stopped") startMicrophone();
            else stopMicrophone();
          }}
        >
          <div className="orb-halo"></div>
          <div className="orb-flare-vertical"></div>
          <div className="orb-flare-horizontal"></div>
          <div className="prismatic-flare prismatic-flare-1"></div>
          <div className="prismatic-flare prismatic-flare-2"></div>
          <div className="prismatic-flare prismatic-flare-3"></div>
          <div className="prismatic-flare prismatic-flare-4"></div>
          <div className="orb-core"></div>
        </div>

        <motion.div
          className="splash-text"
          animate={{ opacity: connectionState === "connected" ? 1 : 0.4 }}
        >
          {connectionState === "connected" ? liveStatus : "CloudTutor Voice"}
        </motion.div>

        <AnimatePresence>
          {connectionState === "connected" && !isArtifactMode && (
            <motion.button
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.95 }}
              className="btn-end-conversation"
              onClick={() => {
                disconnect();
              }}
            >
              END CONVERSATION
            </motion.button>
          )}
        </AnimatePresence>

        <AnimatePresence>
          {awaitingLaunchConfirmation && (
            <motion.div
              className="mission-card launch-card"
              initial={{ opacity: 0, y: 14 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -12 }}
            >
              <h3 className="mission-card-title">Launch Browser Walkthrough?</h3>
              <p className="mission-card-text">
                For safety reasons, approve launch so I can open the browser and guide you step by step.
              </p>
              <div className="mission-card-actions">
                <button
                  className="mission-btn mission-btn-secondary"
                  onClick={() => submitNavigatorDecision(false, "launch")}
                >
                  Not Now
                </button>
                <button
                  className="mission-btn mission-btn-primary"
                  onClick={() => submitNavigatorDecision(true, "launch")}
                >
                  Approve Launch
                </button>
              </div>
              <p className="mission-hint">
                Voice-first: say &quot;yes&quot;, &quot;go for it&quot;, or &quot;show me more&quot;. Button is fallback.
              </p>
            </motion.div>
          )}
        </AnimatePresence>

        <AnimatePresence>
          {isNavigating && (
            <motion.div
              className="mission-console"
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
            >
              <div className="mission-row">
                <span className="mission-label">Navigator</span>
                <span className="mission-value">{stageLabel}</span>
              </div>
              {navigatorTopic && (
                <div className="mission-topic">Topic: &quot;{navigatorTopic}&quot;</div>
              )}
              {navigatorCurrentUrl && (
                <a
                  className="mission-link"
                  href={navigatorCurrentUrl}
                  target="_blank"
                  rel="noreferrer"
                >
                  {navigatorCurrentUrl}
                </a>
              )}

              {navigatorSafety && (
                <div className="mission-safety-card">
                  <h4>Action Requires Approval</h4>
                  <p>{navigatorSafety.explanation || navigatorSafety.action}</p>
                  <div className="mission-card-actions">
                    <button
                      className="mission-btn mission-btn-secondary"
                      onClick={() => submitNavigatorDecision(false, "safety")}
                    >
                      Deny
                    </button>
                    <button
                      className="mission-btn mission-btn-primary"
                      onClick={() => submitNavigatorDecision(true, "safety")}
                    >
                      Allow
                    </button>
                  </div>
                  <p className="mission-hint">Voice shortcut: say &quot;yes&quot; or &quot;no&quot;.</p>
                </div>
              )}

              {navigatorSteps.length > 0 && (
                <div className="mission-section">
                  <h4>Navigator Steps</h4>
                  <ul className="mission-list">
                    {navigatorSteps.slice(-10).map((step) => (
                      <li key={`${step.index}-${step.action}`}>
                        <span>#{step.index}</span>
                        <span>{step.action}</span>
                        <span>{step.status}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {navigatorTimeline.length > 0 && (
                <div className="mission-section">
                  <h4>Timeline</h4>
                  <ul className="mission-log-list">
                    {navigatorTimeline.slice(0, 12).map((entry, idx) => (
                      <li key={`${entry}-${idx}`}>{entry}</li>
                    ))}
                  </ul>
                </div>
              )}

              {isCompleted && latestAgentFinal && (
                <div className="mission-section">
                  <h4>Final Summary</h4>
                  <p className="mission-final">{latestAgentFinal}</p>
                </div>
              )}
            </motion.div>
          )}
        </AnimatePresence>

        <AnimatePresence>
          {connectionState === "disconnected" && !isNavigating && !awaitingLaunchConfirmation && !isArtifactMode && (
            <motion.button
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.95 }}
              className="start-btn"
              onClick={connect}
            >
              AWAKEN CLOUDTUTOR
            </motion.button>
          )}
        </AnimatePresence>

        <AnimatePresence>
          {isArtifactMode && (
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -10 }}
              className="artifact-summary"
            >
              <h2>{artifactResult?.topic || "Your Session Artifact"}</h2>
              <p>
                {artifactResult?.summary || 
                  "We have compiled your interactive session into a downloadable tutorial artifact."}
              </p>
              
              {artifactResult?.key_points && artifactResult.key_points.length > 0 && (
                <ul>
                  {artifactResult.key_points.slice(0, 3).map((point, i) => (
                    <li key={`kp-${i}`}>{point}</li>
                  ))}
                </ul>
              )}

              <div className="artifact-actions">
                {artifactResult?.pdf_url ? (
                  <a href={artifactResult.pdf_url} download className="artifact-btn" target="_blank" rel="noreferrer">
                    Download PDF
                  </a>
                ) : (
                  <button className="artifact-btn" disabled>
                    {artifactBusy ? "Generating..." : "PDF Unavailable"}
                  </button>
                )}

                <button 
                  className="artifact-btn secondary"
                  onClick={() => {
                    setIsArtifactMode(false);
                    setArtifactResult(null);
                    setEventLog([]);
                    setUserTranscript("");
                    setAgentTranscript("");
                  }}
                  style={{ marginTop: "1rem" }}
                >
                  RETURN TO HOME
                </button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

      </motion.div>

      {/* Dynamic Right Panel (Browser view) */}
      <AnimatePresence>
        {isNavigating && (
          <motion.div
            className="right-panel"
            initial={{ width: "0%", opacity: 0 }}
            animate={{ width: "62%", opacity: 1 }}
            exit={{ width: "0%", opacity: 0 }}
            transition={{ duration: 0.8, ease: "anticipate", delay: 0.1 }}
          >
            <div className="browser-panel-header">
              <div className="browser-panel-title">Live Browser Workspace</div>
              <div className="browser-panel-stage">{stageLabel}</div>
            </div>

            <div className="browser-panel-body">
              {navigatorSessionUrl ? (
                <iframe
                  src={navigatorSessionUrl}
                  className="browser-iframe"
                  sandbox="allow-same-origin allow-scripts allow-forms"
                  allow="clipboard-read; clipboard-write"
                  loading="lazy"
                  referrerPolicy="no-referrer"
                />
              ) : (
                <div className="browser-loading">
                  [ INITIALIZING SECURE NAVIGATION DOMAIN ]
                  <br />
                  <span>{stageLabel}</span>
                </div>
              )}

              <AnimatePresence>
                {navigatorSafety && (
                  <motion.div
                    initial={{ y: 20, opacity: 0 }}
                    animate={{ y: 0, opacity: 1 }}
                    exit={{ y: -20, opacity: 0 }}
                    className="browser-overlay-card safety-overlay"
                  >
                    <h3>[ SECURITY CHECK ]</h3>
                    <p>{navigatorSafety.explanation || navigatorSafety.action}</p>
                    <div className="mission-card-actions">
                      <button
                        className="mission-btn mission-btn-secondary"
                        onClick={() => submitNavigatorDecision(false, "safety")}
                      >
                        Deny
                      </button>
                      <button
                        className="mission-btn mission-btn-primary"
                        onClick={() => submitNavigatorDecision(true, "safety")}
                      >
                        Allow
                      </button>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>

              <AnimatePresence>
                {isCompleted && (
                  <motion.div
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    className="browser-overlay-card completion-overlay"
                  >
                    <h3>Task completed</h3>
                    <p>
                      {navigatorTopic
                        ? `Completed walkthrough for "${navigatorTopic}".`
                        : "Documentation walkthrough completed."}
                    </p>
                    {latestAgentFinal && (
                      <p className="completion-answer">{latestAgentFinal}</p>
                    )}
                    <button
                      className="mission-btn mission-btn-primary"
                      onClick={() => resetNavigatorState()}
                    >
                      Return to Voice
                    </button>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {isArtifactMode && (
          <motion.div
            className="right-panel"
            initial={{ width: "0%", opacity: 0 }}
            animate={{ width: "62%", opacity: 1 }}
            exit={{ width: "0%", opacity: 0 }}
            transition={{ duration: 0.8, ease: "anticipate", delay: 0.1 }}
          >
            <div className="browser-panel-header">
              <div className="browser-panel-title">Generated Tutorial Artifact</div>
              <div className="browser-panel-stage">Artifact Ready</div>
            </div>
            <div className="browser-panel-body">
              {artifactBusy ? (
                <div className="browser-loading">
                  [ GENERATING TUTORIAL ARTIFACT ]
                  <br />
                  <span>Synthesizing session context...</span>
                </div>
              ) : artifactError ? (
                <div className="browser-loading" style={{ color: "#ff8b8b" }}>
                  [ ERROR GENERATING ARTIFACT ]
                  <br />
                  <span>{artifactError}</span>
                </div>
              ) : artifactResult && artifactResult.html_url ? (
                <iframe
                  src={artifactResult.html_url}
                  className="browser-iframe"
                  title="Tutorial Artifact"
                />
              ) : (
                <div className="browser-loading">
                  [ WAITING FOR ARTIFACT DATA ]
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Hidden debugging / controls info */}
      <div className="debug-panel">
        <div>connection: {connectionState}</div>
        <div>mic: {micState}</div>
        <div>stage: {navigatorStage}</div>
      </div>
    </div>
  );
}
