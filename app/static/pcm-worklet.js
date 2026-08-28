// AudioWorklet: captures raw Float32 PCM frames for the Live session.
class PCMCapture extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0]) this.port.postMessage(input[0]);
    return true;
  }
}
registerProcessor('pcm-capture', PCMCapture);
