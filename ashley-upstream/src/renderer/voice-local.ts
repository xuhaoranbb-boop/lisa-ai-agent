import type {
  VoiceProvider,
  VoiceProviderAudioInput,
  VoiceProviderAudioTransportStats,
  VoiceProviderEvent,
  VoiceProviderListener,
  VoiceProviderOutputAudio,
  VoiceProviderOutputAudioListener,
  VoiceProviderSessionConfig,
  VoiceResponseRequest
} from './voice-provider';

const DEFAULT_BRIDGE_URL = 'http://127.0.0.1:8765';
const SPEECH_RMS_THRESHOLD = 0.02;
const SPEECH_FRAMES_REQUIRED = 5;
const BARGE_IN_RMS_THRESHOLD = 0.045;
const BARGE_IN_FRAMES_REQUIRED = 8;
const SILENCE_MS = 350;
const STABLE_SILENCE_MS = 260;
const INCOMPLETE_SILENCE_MS = 900;
const MAX_SPEECH_THRESHOLD = 0.12;
const MAX_RECORDING_MS = 10_000;
const RESUME_DELAY_MS = 100;
const ASR_WORKLET_NAME = 'jarvis-pcm16-resampler';
const ASR_FRAME_BYTES = 640;

const STOP_COMMANDS = new Set([
  '停止',
  '停',
  '停下',
  '停一下',
  '取消',
  '取消任务',
  '别说了',
  '别说话了',
  '不要说了',
  '闭嘴',
  '闭嘴滚蛋',
  '滚蛋',
  '住口',
  '安静',
  'stop',
  'shutup',
  'stoptalking',
  'cancel'
]);

const INCOMPLETE_TAILS = [
  '然后',
  '而且',
  '但是',
  '所以',
  '因为',
  '如果',
  '虽然',
  '不过',
  '另外',
  '还有',
  '就是',
  '那个',
  '这个',
  '接着',
  '以及',
  '和',
  '跟',
  '把',
  '被',
  '让',
  '给',
  '在',
  '对',
  '从',
  '到',
  '向',
  '为',
  '为了',
  '关于',
  '至于',
  '比如',
  '例如',
  '包括',
  '尤其',
  '特别是',
  '想要',
  '需要',
  '请',
  '帮我',
  '告诉我',
  '说一下',
  '解释一下'
];

function normalizeCommandText(text: string): string {
  return text.trim().replace(/[\s，。！？!?；;,.]+/gu, '').toLowerCase();
}

function isLikelyIncompleteUtterance(text: string): boolean {
  const trimmed = text.trim();
  if (!trimmed) return false;
  if (/[。！？!?…]$/u.test(trimmed)) return false;
  if (/[，,、：:；;]$/u.test(trimmed)) return true;
  const tail = trimmed.slice(-8);
  return INCOMPLETE_TAILS.some((suffix) => tail.endsWith(suffix));
}

type LocalProviderOptions = {
  log: (message: string) => void;
  verifySpeaker?: () => Promise<boolean>;
};

type LocalBridgeResponse = {
  ok?: boolean;
  error?: string;
  transcript?: string;
  reply?: string;
  tool_calls?: number;
  audio_base64?: string;
  audio_format?: string;
  audio_sample_rate?: number;
  latency?: Record<string, number | string | null>;
};

type LocalStreamEvent = {
  type?: string;
  text?: string;
  delta?: string;
  reply?: string;
  error?: string;
  turn_id?: number;
  audio_base64?: string;
  audio_format?: string;
  audio_sample_rate?: number;
  latency?: Record<string, number | string | null>;
};

function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    const chunk = bytes.subarray(offset, Math.min(offset + chunkSize, bytes.length));
    binary += String.fromCharCode(...chunk);
  }
  return btoa(binary);
}

function base64ToBytes(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

export class LocalTurnVoiceProvider implements VoiceProvider {
  readonly name = 'local' as const;

  private readonly listeners = new Set<VoiceProviderListener>();
  private readonly outputAudioListeners = new Set<VoiceProviderOutputAudioListener>();
  private inputStream: MediaStream | null = null;
  private recorder: MediaRecorder | null = null;
  private recordingTimer: number | null = null;
  private resumeTimer: number | null = null;
  private bridgeUrl = DEFAULT_BRIDGE_URL;
  private sessionConfig: VoiceProviderSessionConfig | null = null;
  private closed = false;
  private listening = false;
  private monitorContext: AudioContext | null = null;
  private monitorSource: MediaStreamAudioSourceNode | null = null;
  private monitorAnalyser: AnalyserNode | null = null;
  private monitorData: Uint8Array<ArrayBuffer> | null = null;
  private monitorFrame: number | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private workletGain: GainNode | null = null;
  private streamingAsrEnabled = false;
  private asrSocket: WebSocket | null = null;
  private asrStartPending = false;
  private asrSessionActive = false;
  private asrIsProbe = false;
  private asrTurnCounter = 0;
  private activeAsrTurnId = 0;
  private asrPartialTimer: number | null = null;
  private speechFrames = 0;
  private silenceStartedAt = 0;
  private noiseFloor = 0;
  private lastPartialText = '';
  private partialStableCount = 0;
  private assistantPlaybackUntil = 0;
  private bargeInGraceUntil = 0;
  private ignoreSpeechUntil = 0;
  private activeRequestId: string | null = null;
  private activeController: AbortController | null = null;
  private conversationSessionId = '';

  constructor(private readonly options: LocalProviderOptions) {}

  subscribe(listener: VoiceProviderListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  receiveAudio(listener: VoiceProviderOutputAudioListener): () => void {
    this.outputAudioListeners.add(listener);
    return () => this.outputAudioListeners.delete(listener);
  }

  async sendAudio(input: VoiceProviderAudioInput): Promise<void> {
    if (!(input instanceof MediaStream)) {
      throw new Error('Local provider requires a MediaStream microphone.');
    }
    this.inputStream = input;
  }

  async establishSession(config: VoiceProviderSessionConfig): Promise<void> {
    this.sessionConfig = config;
    this.closed = false;
    this.conversationSessionId = crypto.randomUUID();
    this.options.log(`Local voice bridge: ${this.bridgeUrl}`);
    this.emit({ type: 'transport.connected' });
    this.emit({ type: 'session.ready' });
    await this.startListening();
  }

  interrupt(options: { clearOutputAudio?: boolean } = {}): boolean {
    const requestId = this.activeRequestId;
    if (requestId) {
      void fetch(`${this.bridgeUrl}/api/cancel`, {
        method: 'POST',
        headers: this.bridgeHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ request_id: requestId, reason: 'User interrupted speech.' })
      }).catch(() => undefined);
    }
    this.activeController?.abort();
    this.cancelAsrSession();
    this.stopRecording();
    this.assistantPlaybackUntil = 0;
    if (options.clearOutputAudio) {
      this.emit({ type: 'assistant.audio.ended' });
    }
    return true;
  }

  submitToolResult(): boolean {
    return false;
  }

  requestResponse(): boolean {
    return true;
  }

  async readOutputAudioTransportStats(): Promise<VoiceProviderAudioTransportStats | null> {
    return null;
  }

  async closeSession(): Promise<void> {
    this.closed = true;
    this.activeController?.abort();
    this.cancelAsrSession();
    this.stopRecording();
    this.stopListening();
    const socket = this.asrSocket;
    this.asrSocket = null;
    socket?.close();
    this.inputStream = null;
    this.sessionConfig = null;
    this.conversationSessionId = '';
  }

  private emit(event: VoiceProviderEvent): void {
    for (const listener of this.listeners) listener(event);
  }

  private emitAudio(audio: VoiceProviderOutputAudio): void {
    for (const listener of this.outputAudioListeners) listener(audio);
  }

  private async startListening(): Promise<void> {
    if (this.closed || !this.inputStream) return;
    if (this.listening) {
      if (this.streamingAsrEnabled) this.ensureAsrSocket();
      return;
    }
    const context = new AudioContext({ latencyHint: 'interactive' });
    const source = context.createMediaStreamSource(this.inputStream);
    const analyser = context.createAnalyser();
    analyser.fftSize = 1_024;
    source.connect(analyser);
    this.monitorContext = context;
    this.monitorSource = source;
    this.monitorAnalyser = analyser;
    this.monitorData = new Uint8Array(analyser.fftSize);
    try {
      const moduleUrl = new URL('./voice-pcm-worklet.js', window.location.href).href;
      await context.audioWorklet.addModule(moduleUrl);
      const worklet = new AudioWorkletNode(context, ASR_WORKLET_NAME, {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        processorOptions: {
          targetRate: 16_000,
          frameBytes: ASR_FRAME_BYTES
        }
      });
      const silentGain = context.createGain();
      silentGain.gain.value = 0;
      source.connect(worklet);
      worklet.connect(silentGain);
      silentGain.connect(context.destination);
      worklet.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
        if (
          this.asrSessionActive
          && this.asrSocket?.readyState === WebSocket.OPEN
        ) {
          this.asrSocket.send(event.data);
        }
      };
      this.workletNode = worklet;
      this.workletGain = silentGain;
      this.streamingAsrEnabled = true;
      this.ensureAsrSocket();
    } catch (error) {
      this.streamingAsrEnabled = false;
      this.options.log(
        `Local ASR worklet unavailable; using segment recorder: ${
          error instanceof Error ? error.message : String(error)
        }`
      );
    }
    await context.resume().catch(() => undefined);
    this.listening = true;
    this.speechFrames = 0;
    this.silenceStartedAt = 0;
    this.lastPartialText = '';
    this.partialStableCount = 0;
    this.monitorFrame = requestAnimationFrame(() => this.monitorAudio());
  }

  private stopListening(): void {
    this.listening = false;
    if (this.monitorFrame !== null) {
      cancelAnimationFrame(this.monitorFrame);
      this.monitorFrame = null;
    }
    this.monitorSource?.disconnect();
    this.monitorSource = null;
    this.monitorAnalyser = null;
    this.monitorData = null;
    this.workletNode?.port.close();
    this.workletNode?.disconnect();
    this.workletNode = null;
    this.workletGain?.disconnect();
    this.workletGain = null;
    this.streamingAsrEnabled = false;
    if (this.monitorContext && this.monitorContext.state !== 'closed') {
      void this.monitorContext.close();
    }
    this.monitorContext = null;
  }

  private monitorAudio(): void {
    if (!this.listening || this.closed || !this.monitorAnalyser || !this.monitorData) return;
    const now = performance.now();
    this.monitorAnalyser.getByteTimeDomainData(this.monitorData);

    let sum = 0;
    for (const value of this.monitorData) {
      const normalized = (value - 128) / 128;
      sum += normalized * normalized;
    }
    const rms = Math.sqrt(sum / this.monitorData.length);
    const speechThreshold = this.speechThreshold();
    const bargeInThreshold = Math.max(speechThreshold, BARGE_IN_RMS_THRESHOLD);
    const silenceThreshold = Math.max(SPEECH_RMS_THRESHOLD, speechThreshold * 0.75);
    const speechActive = rms >= speechThreshold;
    const bargeInActive = rms >= bargeInThreshold;
    const silenceActive = rms < silenceThreshold;

    // Learn the room's noise floor only while nothing is being transcribed.
    // This stops a fan, air conditioner, or distant chatter from becoming a
    // permanent speech trigger without raising the bar during real speech.
    if (!this.asrSessionActive && !speechActive) this.updateNoiseFloor(rms);

    if (now < this.ignoreSpeechUntil) {
      this.speechFrames = 0;
    } else if (this.streamingAsrEnabled) {
      if (this.asrSessionActive) {
        if (silenceActive) {
          if (this.silenceStartedAt === 0) this.silenceStartedAt = now;
          if (now - this.silenceStartedAt >= this.endpointSilenceMs()) {
            void this.stopAsrSession();
          }
        } else {
          this.silenceStartedAt = 0;
        }
      } else if (bargeInActive && now < this.assistantPlaybackUntil) {
        this.speechFrames += 1;
        if (this.speechFrames >= BARGE_IN_FRAMES_REQUIRED) {
          this.speechFrames = 0;
          this.beginBargeIn();
        }
      } else if (speechActive) {
        this.speechFrames += 1;
        if (this.speechFrames >= SPEECH_FRAMES_REQUIRED) {
          this.speechFrames = 0;
          void this.startAsrSession();
        }
      } else {
        this.speechFrames = 0;
      }
    } else if (this.recorder && this.recorder.state !== 'inactive') {
      if (silenceActive) {
        if (this.silenceStartedAt === 0) this.silenceStartedAt = now;
        if (now - this.silenceStartedAt >= this.endpointSilenceMs()) this.stopRecording();
      } else {
        this.silenceStartedAt = 0;
      }
    } else if (bargeInActive && performance.now() < this.assistantPlaybackUntil) {
      this.speechFrames = 0;
    } else if (speechActive) {
      this.speechFrames += 1;
      if (this.speechFrames >= SPEECH_FRAMES_REQUIRED) {
        this.speechFrames = 0;
        this.startRecording();
      }
    } else {
      this.speechFrames = 0;
    }

    this.monitorFrame = requestAnimationFrame(() => this.monitorAudio());
  }

  private speechThreshold(): number {
    const adaptive = this.noiseFloor * 2.5 + 0.012;
    return Math.min(MAX_SPEECH_THRESHOLD, Math.max(SPEECH_RMS_THRESHOLD, adaptive));
  }

  private updateNoiseFloor(rms: number): void {
    if (!Number.isFinite(rms)) return;
    if (this.noiseFloor <= 0) {
      this.noiseFloor = rms;
      return;
    }
    const alpha = rms < this.noiseFloor ? 0.2 : 0.02;
    this.noiseFloor = this.noiseFloor * (1 - alpha) + rms * alpha;
  }

  private endpointSilenceMs(): number {
    const text = this.lastPartialText.trim();
    if (!text) return SILENCE_MS;
    if (STOP_COMMANDS.has(normalizeCommandText(text))) return SILENCE_MS;
    if (isLikelyIncompleteUtterance(text)) return INCOMPLETE_SILENCE_MS;
    return this.partialStableCount >= 2 ? STABLE_SILENCE_MS : SILENCE_MS;
  }

  private beginBargeIn(): void {
    // During assistant playback, only an explicit stop command may cancel the
    // reply. Keep the assistant audible while ASR checks the utterance text.
    void this.startAsrSession(false);
  }

  private startRecording(): void {
    if (!this.inputStream || this.closed) return;
    if (this.recorder && this.recorder.state !== 'inactive') return;

    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : 'audio/webm';
    const recorder = new MediaRecorder(this.inputStream, { mimeType });
    const chunks: Blob[] = [];
    this.recorder = recorder;
    this.silenceStartedAt = 0;
    recorder.addEventListener('dataavailable', (event) => {
      if (event.data.size > 0) chunks.push(event.data);
    });
    recorder.addEventListener('stop', () => void this.finishRecording(chunks, mimeType));
    this.emit({ type: 'user.speech.started' });
    recorder.start();
    this.recordingTimer = window.setTimeout(() => this.stopRecording(), MAX_RECORDING_MS);
  }

  private stopRecording(): void {
    if (this.recordingTimer !== null) {
      window.clearTimeout(this.recordingTimer);
      this.recordingTimer = null;
    }
    if (this.recorder && this.recorder.state !== 'inactive') {
      this.recorder.stop();
    }
  }

  private ensureAsrSocket(): void {
    if (
      this.closed
      || !this.streamingAsrEnabled
      || this.asrSocket?.readyState === WebSocket.OPEN
      || this.asrSocket?.readyState === WebSocket.CONNECTING
    ) {
      return;
    }
    const token = this.sessionConfig?.bridgeToken?.trim();
    const socketUrl = `${this.bridgeUrl.replace(/^http/, 'ws')}/ws/asr${
      token ? `?token=${encodeURIComponent(token)}` : ''
    }`;
    const socket = new WebSocket(socketUrl);
    this.asrSocket = socket;
    socket.onopen = () => {
      if (this.asrSocket !== socket) return;
      if (this.asrSessionActive || this.asrStartPending) {
        this.asrStartPending = false;
        socket.send(JSON.stringify({ type: 'start', turn_id: this.activeAsrTurnId }));
      }
    };
    socket.onmessage = async (event) => {
      if (this.asrSocket !== socket) return;
      try {
        const message = JSON.parse(String(event.data)) as {
          type?: string;
          text?: string;
          turn_id?: number;
        };
        const turnId = typeof message.turn_id === 'number' ? message.turn_id : null;
        if (turnId !== null && turnId !== this.activeAsrTurnId) return;
        if (message.type === 'partial' && message.text) {
          if (message.text === this.lastPartialText) {
            this.partialStableCount += 1;
          } else {
            this.lastPartialText = message.text;
            this.partialStableCount = 1;
          }
          this.emit({
            type: 'transcription',
            speaker: 'user',
            source: 'audio',
            text: message.text,
            final: false
          });
          await this.handleImmediateCommand(message.text, false);
        } else if (message.type === 'final') {
          const finalTurnId = this.activeAsrTurnId;
          this.asrSessionActive = false;
          this.asrStartPending = false;
          if (this.asrPartialTimer !== null) {
            window.clearTimeout(this.asrPartialTimer);
            this.asrPartialTimer = null;
          }
          const text = message.text || '';
          this.lastPartialText = '';
          this.partialStableCount = 0;
          this.emit({
            type: 'transcription',
            speaker: 'user',
            source: 'audio',
            text,
            final: true
          });
          const handled = await this.handleImmediateCommand(text, true);
          if (this.activeAsrTurnId === finalTurnId) this.activeAsrTurnId = 0;
          if (handled) return;
          void this.respondToText(text);
        }
      } catch (error) {
        this.emit({
          type: 'error',
          message: error instanceof Error ? error.message : String(error)
        });
      }
    };
    socket.onerror = () => {
      if (this.asrSocket === socket) {
        this.emit({ type: 'error', message: 'Local streaming ASR socket failed.' });
      }
    };
    socket.onclose = () => {
      if (this.asrSocket !== socket) return;
      this.asrSocket = null;
      this.asrSessionActive = false;
      this.asrStartPending = false;
      this.activeAsrTurnId = 0;
      if (!this.closed) {
        this.scheduleListeningResume(500);
      }
    };
  }

  private async startAsrSession(announce = true): Promise<void> {
    if (this.asrSessionActive || this.closed) return;
    this.asrSessionActive = true;
    this.asrIsProbe = !announce;
    this.asrTurnCounter += 1;
    this.activeAsrTurnId = this.asrTurnCounter;
    this.lastPartialText = '';
    this.partialStableCount = 0;
    this.silenceStartedAt = 0;
    if (announce) {
      this.emit({
        type: 'diagnostic',
        message:
          `Local VAD threshold=${this.speechThreshold().toFixed(4)} `
          + `noise=${this.noiseFloor.toFixed(4)}.`
      });
      this.emit({ type: 'user.speech.started' });
    }
    this.ensureAsrSocket();
    if (this.asrSocket?.readyState === WebSocket.OPEN) {
      this.asrSocket.send(
        JSON.stringify({ type: 'start', turn_id: this.activeAsrTurnId })
      );
    } else {
      this.asrStartPending = true;
    }
    if (this.asrPartialTimer !== null) window.clearTimeout(this.asrPartialTimer);
    this.asrPartialTimer = window.setTimeout(
      () => void this.stopAsrSession(),
      MAX_RECORDING_MS
    );
  }

  private async stopAsrSession(): Promise<void> {
    if (!this.asrSessionActive) return;
    this.asrSessionActive = false;
    this.asrStartPending = false;
    if (this.asrPartialTimer !== null) {
      window.clearTimeout(this.asrPartialTimer);
      this.asrPartialTimer = null;
    }
    this.lastPartialText = '';
    this.partialStableCount = 0;
    if (!this.asrIsProbe) this.emit({ type: 'user.speech.ended' });
    if (this.asrSocket?.readyState === WebSocket.OPEN) {
      this.asrSocket.send(JSON.stringify({ type: 'stop' }));
    } else {
      this.scheduleListeningResume(500);
    }
  }

  private cancelAsrSession(): void {
    this.asrSessionActive = false;
    this.asrStartPending = false;
    this.activeAsrTurnId = 0;
    this.lastPartialText = '';
    this.partialStableCount = 0;
    if (this.asrPartialTimer !== null) {
      window.clearTimeout(this.asrPartialTimer);
      this.asrPartialTimer = null;
    }
    if (this.asrSocket?.readyState === WebSocket.OPEN) {
      this.asrSocket.send(JSON.stringify({ type: 'cancel' }));
    }
  }

  private async handleImmediateCommand(text: string, final: boolean): Promise<boolean> {
    const normalized = normalizeCommandText(text);
    if (!STOP_COMMANDS.has(normalized)) {
      if (performance.now() < this.assistantPlaybackUntil) {
        if (final) this.ignoreSpeechUntil = performance.now() + 450;
        this.emit({
          type: 'diagnostic',
          message: 'Ignored non-stop speech during assistant playback.'
        });
        this.scheduleListeningResume(120);
        return true;
      }
      return false;
    }
    if (!(await this.speakerAllowed())) {
      this.emit({
        type: 'diagnostic',
        message: 'Stop command rejected by personal voiceprint.'
      });
      this.scheduleListeningResume(120);
      return true;
    }
    this.ignoreSpeechUntil = performance.now() + 650;
    this.interrupt({ clearOutputAudio: true });
    this.emit({
      type: 'assistant.response.ended',
      status: 'cancelled',
      reason: final ? 'stop-command' : 'stop-command-partial'
    });
    this.scheduleListeningResume(120);
    return true;
  }

  private async respondToText(text: string): Promise<void> {
    if (!text.trim() || this.closed) {
      this.scheduleListeningResume(300);
      return;
    }
    if (performance.now() < this.assistantPlaybackUntil) {
      this.emit({
        type: 'diagnostic',
        message: '丽萨正在说话时，忽略了一句非指令语音。'
      });
      this.scheduleListeningResume(120);
      return;
    }
    if (!(await this.speakerAllowed())) {
      this.emit({ type: 'diagnostic', message: 'Local turn rejected by personal voiceprint.' });
      this.scheduleListeningResume(260);
      return;
    }
    const sessionId = this.conversationSessionId || crypto.randomUUID();
    const requestId = crypto.randomUUID();
    const controller = new AbortController();
    this.activeRequestId = requestId;
    this.activeController = controller;
    try {
      const streamed = await this.finishRecordingStreamed(
        '',
        sessionId,
        requestId,
        controller,
        text
      );
      if (!streamed) {
        await this.finishRecordingJson(
          '',
          sessionId,
          requestId,
          controller,
          text
        );
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        this.emit({
          type: 'assistant.response.ended',
          responseId: sessionId,
          status: 'cancelled',
          reason: 'user'
        });
        this.scheduleListeningResume(RESUME_DELAY_MS);
        return;
      }
      this.emit({
        type: 'error',
        message: error instanceof Error ? error.message : String(error)
      });
      this.scheduleListeningResume(1_000);
    } finally {
      if (this.activeRequestId === requestId) {
        this.activeRequestId = null;
        this.activeController = null;
      }
    }
  }

  private scheduleListeningResume(delayMs: number): void {
    if (this.resumeTimer !== null) window.clearTimeout(this.resumeTimer);
    this.resumeTimer = window.setTimeout(() => {
      this.resumeTimer = null;
      if (!this.closed) void this.startListening();
    }, delayMs);
  }

  private async speakerAllowed(): Promise<boolean> {
    if (!this.options.verifySpeaker) return true;
    try {
      return await this.options.verifySpeaker();
    } catch (error) {
      this.options.log(
        `Personal voiceprint verification failed closed: ${
          error instanceof Error ? error.message : String(error)
        }`
      );
      return false;
    }
  }

  private async finishRecording(chunks: Blob[], mimeType: string): Promise<void> {
    this.recorder = null;
    this.emit({ type: 'user.speech.ended' });
    if (chunks.length === 0) {
      this.scheduleListeningResume(300);
      return;
    }
    if (!(await this.speakerAllowed())) {
      this.emit({ type: 'diagnostic', message: 'Local turn rejected by personal voiceprint.' });
      this.scheduleListeningResume(260);
      return;
    }

    const blob = new Blob(chunks, { type: mimeType });
    const audioBase64 = bytesToBase64(new Uint8Array(await blob.arrayBuffer()));
    const sessionId = this.conversationSessionId || crypto.randomUUID();
    const requestId = crypto.randomUUID();
    const controller = new AbortController();
    this.activeRequestId = requestId;
    this.activeController = controller;

    try {
      const streamed = await this.finishRecordingStreamed(
        audioBase64,
        sessionId,
        requestId,
        controller,
        undefined
      );
      if (!streamed) {
        await this.finishRecordingJson(
          audioBase64,
          sessionId,
          requestId,
          controller,
          undefined
        );
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        this.emit({
          type: 'assistant.response.ended',
          responseId: sessionId,
          status: 'cancelled',
          reason: 'user'
        });
        this.scheduleListeningResume(RESUME_DELAY_MS);
        return;
      }
      this.emit({
        type: 'error',
        message: error instanceof Error ? error.message : String(error)
      });
      this.scheduleListeningResume(1_000);
    } finally {
      if (this.activeRequestId === requestId) {
        this.activeRequestId = null;
        this.activeController = null;
      }
    }
  }

  private async finishRecordingJson(
    audioBase64: string,
    sessionId: string,
    requestId: string,
    controller: AbortController,
    text?: string
  ): Promise<void> {
    const response = await fetch(`${this.bridgeUrl}/api/respond`, {
      method: 'POST',
      headers: this.bridgeHeaders({ 'Content-Type': 'application/json' }),
      signal: controller.signal,
      body: JSON.stringify(
        text
          ? { text, session_id: sessionId, request_id: requestId }
          : { audio_base64: audioBase64, session_id: sessionId, request_id: requestId }
      )
    });
    const payload = (await response.json()) as LocalBridgeResponse;
    if (this.activeRequestId !== requestId) return;
    if (!response.ok || !payload.ok || !payload.reply) {
      throw new Error(payload.error || `Bridge HTTP ${response.status}`);
    }
    if (payload.latency) {
      this.options.log(
        `[VOICE] path=${String(payload.latency.path ?? 'llm')} `
        + `ASR=${String(payload.latency.asr_ms ?? 'n/a')}ms `
        + `Router=${String(payload.latency.router_ms ?? 'n/a')}ms `
        + `LLM first token=${String(payload.latency.llm_ttft_ms ?? 'n/a')}ms `
        + `TTS first audio=${String(payload.latency.tts_ttfa_ms ?? 'n/a')}ms `
        + `TOTAL=${String(payload.latency.total_ms ?? 'n/a')}ms`
      );
    }

    this.emit({
      type: 'transcription',
      speaker: 'user',
      source: 'audio',
      text: payload.transcript || '',
      final: true
    });
    this.emit({ type: 'assistant.response.started', responseId: sessionId });
    this.emit({ type: 'assistant.audio.started' });
    this.emit({
      type: 'transcription',
      speaker: 'assistant',
      source: 'text',
      text: payload.reply,
      final: true
    });

    let playbackMs = 1_500;
    if (payload.audio_base64) {
      const audioBytes = base64ToBytes(payload.audio_base64);
      const sampleRate = payload.audio_sample_rate ?? 24_000;
      playbackMs = Math.max(1_500, (audioBytes.length / 2 / sampleRate) * 1_000);
      const now = performance.now();
      this.assistantPlaybackUntil = now + playbackMs + 250;
      this.bargeInGraceUntil = now + 250;
      this.emitAudio({
        kind: 'chunk',
        data: audioBytes,
        sampleRate,
        format: payload.audio_format || 'wav'
      });
    }

    this.emit({ type: 'assistant.audio.ended' });
    this.emit({
      type: 'assistant.response.ended',
      responseId: sessionId,
      status: 'completed',
      reason: 'stop',
      outputTokens: payload.tool_calls
    });
    this.scheduleListeningResume(RESUME_DELAY_MS);
  }

  private async finishRecordingStreamed(
    audioBase64: string,
    sessionId: string,
    requestId: string,
    controller: AbortController,
    text?: string
  ): Promise<boolean> {
    const response = await fetch(`${this.bridgeUrl}/api/respond/stream`, {
      method: 'POST',
      headers: this.bridgeHeaders({ 'Content-Type': 'application/json' }),
      signal: controller.signal,
      body: JSON.stringify(
        text
          ? { text, session_id: sessionId, request_id: requestId }
          : { audio_base64: audioBase64, session_id: sessionId, request_id: requestId }
      )
    });
    const contentType = response.headers.get('content-type') || '';
    if (!response.ok || !response.body || !contentType.includes('application/x-ndjson')) {
      return false;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let reply = '';
    let playbackMs = 0;
    let responseStarted = false;
    let audioStarted = false;

    const handleEvent = (event: LocalStreamEvent) => {
      if (this.activeRequestId !== requestId) return;
      if (event.type === 'transcript') {
        this.emit({
          type: 'transcription',
          speaker: 'user',
          source: 'audio',
          text: event.text || '',
          final: true
        });
      } else if (event.type === 'text_delta') {
        reply += event.delta || '';
        this.emit({
          type: 'transcription',
          speaker: 'assistant',
          source: 'text',
          text: event.delta || '',
          final: false
        });
      } else if (event.type === 'audio_chunk') {
        if (!responseStarted) {
          this.emit({ type: 'assistant.response.started', responseId: sessionId });
          responseStarted = true;
        }
        if (!audioStarted) {
          this.emit({ type: 'assistant.audio.started' });
          audioStarted = true;
        }
        const audioBytes = base64ToBytes(event.audio_base64 || '');
        const sampleRate = event.audio_sample_rate ?? 24_000;
        playbackMs += (audioBytes.length / 2 / sampleRate) * 1_000;
        const now = performance.now();
        if (now >= this.assistantPlaybackUntil) this.bargeInGraceUntil = now + 250;
        this.assistantPlaybackUntil = now + playbackMs + 250;
        this.emitAudio({
          kind: 'chunk',
          data: audioBytes,
          sampleRate,
          format: event.audio_format || 'wav'
        });
      } else if (event.type === 'done') {
        reply = event.reply || reply;
        const latency = event.latency;
        if (latency) {
          this.options.log(
            `[VOICE] path=${String(latency.path ?? 'llm')} `
            + `ASR=${String(latency.asr_ms ?? 'n/a')}ms `
            + `Router=${String(latency.router_ms ?? 'n/a')}ms `
            + `LLM first token=${String(latency.llm_ttft_ms ?? 'n/a')}ms `
            + `TTS first audio=${String(latency.tts_ttfa_ms ?? 'n/a')}ms `
            + `TOTAL=${String(latency.total_ms ?? 'n/a')}ms`
          );
        }
      } else if (event.type === 'cancelled') {
        this.emit({
          type: 'assistant.response.ended',
          responseId: sessionId,
          status: 'cancelled',
          reason: 'user'
        });
        this.scheduleListeningResume(RESUME_DELAY_MS);
      } else if (event.type === 'error') {
        throw new Error(event.error || 'Bridge stream failed.');
      }
    };

    let cancelled = false;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (this.activeRequestId !== requestId) {
        cancelled = true;
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let newline = buffer.indexOf('\n');
      while (newline >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (line) {
          const event = JSON.parse(line) as LocalStreamEvent;
          handleEvent(event);
          if (event.type === 'cancelled') cancelled = true;
        }
        newline = buffer.indexOf('\n');
      }
      if (cancelled) break;
    }

    if (cancelled) return true;
    if (audioStarted) this.emit({ type: 'assistant.audio.ended' });
    this.emit({
      type: 'transcription',
      speaker: 'assistant',
      source: 'text',
      text: reply,
      final: true
    });
    if (responseStarted) {
      this.emit({
        type: 'assistant.response.ended',
        responseId: sessionId,
        status: 'completed',
        reason: 'stop'
      });
    }
    this.scheduleListeningResume(RESUME_DELAY_MS);
    return true;
  }

  private bridgeHeaders(base: Record<string, string> = {}): Record<string, string> {
    const token = this.sessionConfig?.bridgeToken?.trim();
    return token ? { ...base, 'X-Jarvis-Token': token } : base;
  }
}
