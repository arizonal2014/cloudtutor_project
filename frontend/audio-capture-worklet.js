class PcmCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0]) {
      // Copy the current frame before posting to avoid buffer reuse side effects.
      this.port.postMessage(new Float32Array(input[0]));
    }
    return true;
  }
}

registerProcessor("pcm-capture-processor", PcmCaptureProcessor);
