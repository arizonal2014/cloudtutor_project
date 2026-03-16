const state = {
  socket: null,
  audioCapture: {
    stream: null,
    context: null,
    source: null,
    processor: null,
    workletNode: null,
    silenceGain: null,
  },
  audioPlayback: {
    context: null,
    nextStartTime: 0,
    pendingBinaryMetadata: [],
  },
};

const backendUrlInput = document.getElementById("backendUrl");
const userIdInput = document.getElementById("userId");
const sessionIdInput = document.getElementById("sessionId");
const statusEl = document.getElementById("status");
const micStatusEl = document.getElementById("micStatus");
const eventLog = document.getElementById("eventLog");
const messageInput = document.getElementById("messageInput");
const userTranscriptEl = document.getElementById("userTranscript");
const agentTranscriptEl = document.getElementById("agentTranscript");

const connectBtn = document.getElementById("connectBtn");
const disconnectBtn = document.getElementById("disconnectBtn");
const pingBtn = document.getElementById("pingBtn");
const sendBtn = document.getElementById("sendBtn");
const startMicBtn = document.getElementById("startMicBtn");
const stopMicBtn = document.getElementById("stopMicBtn");

function setConnected(connected) {
  statusEl.textContent = connected ? "connected" : "disconnected";
  statusEl.classList.toggle("connected", connected);
  connectBtn.disabled = connected;
  disconnectBtn.disabled = !connected;
  pingBtn.disabled = !connected;
  sendBtn.disabled = !connected;
  startMicBtn.disabled = !connected || isMicActive();
  stopMicBtn.disabled = !isMicActive();
}

function setMicStatus(active) {
  micStatusEl.textContent = active ? "active" : "stopped";
  micStatusEl.classList.toggle("connected", active);
  startMicBtn.disabled = !state.socket || active;
  stopMicBtn.disabled = !active;
}

function isMicActive() {
  return Boolean(state.audioCapture.processor || state.audioCapture.workletNode);
}

function logEvent(label, payload) {
  const line = `[${new Date().toISOString()}] ${label}\n${JSON.stringify(payload, null, 2)}\n\n`;
  eventLog.textContent = line + eventLog.textContent;
}

function appendTranscript(target, text) {
  if (!text) {
    return;
  }
  const line = `[${new Date().toLocaleTimeString()}] ${text}\n`;
  target.textContent += line;
  target.scrollTop = target.scrollHeight;
}

function buildWsUrl(httpUrl, userId, sessionId) {
  const base = new URL(httpUrl);
  const protocol = base.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${base.host}/ws/${encodeURIComponent(userId)}/${encodeURIComponent(sessionId)}`;
}

function sendJson(payload) {
  if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
    return;
  }
  state.socket.send(JSON.stringify(payload));
}

function floatTo16BitPCM(float32Array) {
  const pcm = new Int16Array(float32Array.length);
  for (let i = 0; i < float32Array.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, float32Array[i]));
    pcm[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return pcm;
}

function pcm16ToLittleEndianBytes(pcm16) {
  const buffer = new ArrayBuffer(pcm16.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < pcm16.length; i += 1) {
    view.setInt16(i * 2, pcm16[i], true);
  }
  return new Uint8Array(buffer);
}

function downsampleBuffer(buffer, inputSampleRate, outputSampleRate) {
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
      accum += buffer[i];
      count += 1;
    }

    result[offsetResult] = count > 0 ? accum / count : 0;
    offsetResult += 1;
    offsetBuffer = nextOffsetBuffer;
  }

  return result;
}

function base64ToBytes(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

function parseSampleRate(mimeType) {
  const match = /rate=(\d+)/i.exec(mimeType || "");
  if (!match) {
    return 24000;
  }
  const parsed = Number.parseInt(match[1], 10);
  return Number.isFinite(parsed) ? parsed : 24000;
}

function ensurePlaybackContext() {
  if (!state.audioPlayback.context) {
    state.audioPlayback.context = new AudioContext();
    state.audioPlayback.nextStartTime = state.audioPlayback.context.currentTime;
  }
  if (state.audioPlayback.context.state === "suspended") {
    state.audioPlayback.context.resume();
  }
  return state.audioPlayback.context;
}

function playPcmBytes(bytes, mimeType) {
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
  source.connect(context.destination);

  const startAt = Math.max(context.currentTime + 0.02, state.audioPlayback.nextStartTime);
  source.start(startAt);
  state.audioPlayback.nextStartTime = startAt + audioBuffer.duration;
}

function playPcmChunk(base64Audio, mimeType) {
  const bytes = base64ToBytes(base64Audio);
  playPcmBytes(bytes, mimeType);
}

async function handleBinarySocketFrame(data) {
  let bytes;
  if (data instanceof ArrayBuffer) {
    bytes = new Uint8Array(data);
  } else if (data instanceof Blob) {
    bytes = new Uint8Array(await data.arrayBuffer());
  } else {
    return;
  }

  const metadata =
    state.audioPlayback.pendingBinaryMetadata.shift() || { mime_type: "audio/pcm;rate=24000" };
  playPcmBytes(bytes, metadata.mime_type);
}

async function startMicrophone() {
  if (!state.socket || state.socket.readyState !== WebSocket.OPEN || isMicActive()) {
    return;
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const context = new AudioContext();
    const source = context.createMediaStreamSource(stream);
    let processor = null;
    let workletNode = null;
    let silenceGain = null;

    const sendFloatSamples = (input) => {
      if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
        return;
      }

      const downsampled = downsampleBuffer(input, context.sampleRate, 16000);
      const pcm16 = floatTo16BitPCM(downsampled);
      const littleEndianBytes = pcm16ToLittleEndianBytes(pcm16);

      // Send binary PCM chunks to reduce JSON/base64 overhead.
      state.socket.send(littleEndianBytes.buffer);
    };

    if (context.audioWorklet && typeof AudioWorkletNode !== "undefined") {
      try {
        await context.audioWorklet.addModule("audio-capture-worklet.js");
        workletNode = new AudioWorkletNode(context, "pcm-capture-processor");
        workletNode.port.onmessage = (workletEvent) => {
          if (workletEvent?.data instanceof Float32Array) {
            sendFloatSamples(workletEvent.data);
          }
        };
        silenceGain = context.createGain();
        silenceGain.gain.value = 0;
        source.connect(workletNode);
        workletNode.connect(silenceGain);
        silenceGain.connect(context.destination);
      } catch (workletError) {
        logEvent("mic_worklet_fallback", {
          message: workletError?.message || "AudioWorklet unavailable; using ScriptProcessor",
        });
      }
    }

    if (!workletNode) {
      processor = context.createScriptProcessor(4096, 1, 1);
      processor.onaudioprocess = (event) => {
        sendFloatSamples(event.inputBuffer.getChannelData(0));
      };
      source.connect(processor);
      processor.connect(context.destination);
    }

    state.audioCapture = {
      stream,
      context,
      source,
      processor,
      workletNode,
      silenceGain,
    };

    setMicStatus(true);
    logEvent("mic_start", {
      sampleRate: context.sampleRate,
      sendRate: 16000,
      captureMode: workletNode ? "audio_worklet" : "script_processor",
    });
  } catch (error) {
    logEvent("mic_error", { message: error?.message || "Failed to start microphone" });
  }
}

function stopMicrophone() {
  const { stream, source, processor, workletNode, silenceGain, context } = state.audioCapture;

  if (processor) {
    processor.disconnect();
  }
  if (workletNode) {
    workletNode.disconnect();
  }
  if (silenceGain) {
    silenceGain.disconnect();
  }
  if (source) {
    source.disconnect();
  }
  if (stream) {
    stream.getTracks().forEach((track) => track.stop());
  }
  if (context) {
    context.close();
  }

  state.audioCapture = {
    stream: null,
    context: null,
    source: null,
    processor: null,
    workletNode: null,
    silenceGain: null,
  };
  state.audioPlayback.pendingBinaryMetadata = [];

  setMicStatus(false);
  logEvent("mic_stop", { message: "Microphone stopped" });
}

function handleSocketMessage(rawData) {
  let message;
  try {
    message = JSON.parse(rawData);
  } catch {
    logEvent("socket_message_raw", { data: rawData });
    return;
  }

  logEvent("socket_message", message);

  if (message.type === "error") {
    return;
  }

  if (message.input_transcription?.text && message.input_transcription.is_final) {
    appendTranscript(userTranscriptEl, message.input_transcription.text);
  }

  if (message.output_transcription?.text && message.output_transcription.is_final) {
    appendTranscript(agentTranscriptEl, message.output_transcription.text);
  }

  if (Array.isArray(message.parts)) {
    for (const part of message.parts) {
      if (part.type === "audio/pcm" && part.stream === "binary") {
        state.audioPlayback.pendingBinaryMetadata.push({
          mime_type: part.mime_type || "audio/pcm;rate=24000",
        });
        continue;
      }

      if (part.type === "audio/pcm" && part.data) {
        playPcmChunk(part.data, part.mime_type || "audio/pcm;rate=24000");
      }
    }
  }
}

connectBtn.addEventListener("click", () => {
  if (state.socket) {
    return;
  }

  const wsUrl = buildWsUrl(
    backendUrlInput.value.trim(),
    userIdInput.value.trim(),
    sessionIdInput.value.trim(),
  );

  const socket = new WebSocket(wsUrl);
  socket.binaryType = "arraybuffer";

  socket.onopen = () => {
    state.socket = socket;
    setConnected(true);
    logEvent("socket_open", { wsUrl });
  };

  socket.onmessage = (event) => {
    if (typeof event.data === "string") {
      handleSocketMessage(event.data);
      return;
    }

    handleBinarySocketFrame(event.data).catch((error) => {
      logEvent("socket_binary_error", {
        message: error?.message || "Failed to handle binary audio frame",
      });
    });
  };

  socket.onerror = () => {
    logEvent("socket_error", { message: "WebSocket error" });
  };

  socket.onclose = () => {
    stopMicrophone();
    logEvent("socket_close", { message: "WebSocket closed" });
    state.socket = null;
    setConnected(false);
  };
});

disconnectBtn.addEventListener("click", () => {
  if (state.socket) {
    state.socket.close();
  }
});

pingBtn.addEventListener("click", () => {
  if (!state.socket) {
    return;
  }
  const payload = { type: "ping" };
  sendJson(payload);
  logEvent("client_send", payload);
});

sendBtn.addEventListener("click", () => {
  if (!state.socket) {
    return;
  }
  const text = messageInput.value.trim();
  if (!text) {
    return;
  }
  const payload = { mime_type: "text/plain", data: text };
  sendJson(payload);
  logEvent("client_send", payload);
  messageInput.value = "";
});

startMicBtn.addEventListener("click", () => {
  startMicrophone();
});

stopMicBtn.addEventListener("click", () => {
  stopMicrophone();
});

setConnected(false);
setMicStatus(false);
