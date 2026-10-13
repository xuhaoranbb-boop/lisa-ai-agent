type JarvisState = 'idle' | 'listening' | 'thinking' | 'speaking';
type JarvisGesture =
  | 'turn'
  | 'nod'
  | 'shake'
  | 'spin'
  | 'spin-clockwise'
  | 'spin-counterclockwise'
  | 'face-front'
  | 'face-back'
  | 'face-left'
  | 'face-right';
type JarvisAnchor = { x: number; y: number; width: number; height: number };
type VoiceProviderSelection = {
  requestedName: string;
  selectedName: 'openai' | 'doubao' | 'local';
  fallbackReason: string | null;
};
type DoubaoTransportState =
  | { type: 'disconnected'; code: number; reason: string }
  | { type: 'failed'; message: string };
type DesktopSettings = {
  closeToTray: boolean;
  muted: boolean;
  paused: boolean;
  hotkeys: {
    wake: string;
    stop: string;
    pause: string;
    resume: string;
  };
};

interface JarvisRendererApi {
  onState(callback: (state: string) => void): void;
  onLevel(callback: (level: number) => void): void;
  onVisible(callback: (visible: boolean) => void): void;
  onAssemble(callback: () => void): void;
  onAssemblyPresented(callback: () => void): void;
  onAnchor(callback: (anchor: JarvisAnchor) => void): void;
  onGesture(callback: (gesture: JarvisGesture) => void): void;
  onRuntimeEvent(callback: (event: unknown) => void): () => void;
  onWakeEnrollmentUpdate(callback: (update: {
    state: 'idle' | 'ready' | 'listening' | 'captured' | 'complete' | 'error';
    captured: number;
    required: number;
    message: string;
  }) => void): () => void;
  onWakeEnrollmentCommand(callback: (command: 'start' | 'capture' | 'cancel') => void): () => void;
  wakeEnrollmentCommand(command: 'start' | 'capture' | 'cancel'): void;
  reportWakeEnrollmentUpdate(update: {
    state: 'idle' | 'ready' | 'listening' | 'captured' | 'complete' | 'error';
    captured: number;
    required: number;
    message: string;
  }): void;
  getPersonalWakeModel(): Promise<import('./personal-wake').PersonalWakeModel | null>;
  savePersonalWakeModel(model: import('./personal-wake').PersonalWakeModel): Promise<void>;
  onPersonalWakeModelUpdated(callback: (model: unknown) => void): () => void;
  wakeDetected(): void;
  assemblyReady(): void;
  assemblyPresented(): void;
  conversationEnded(): void;
  reportState(state: JarvisState): void;
  reportLevel(level: number): void;
  reportGesture(gesture: JarvisGesture): void;
  reportAudioDiagnostic(summary: string): void;
  reportVoiceEvent(message: string): void;
  reportVoiceTranscript(text: string, speaker: 'user' | 'assistant', final: boolean): void;
  onVoiceTranscript(callback: (payload: { text: string; speaker: 'user' | 'assistant'; final: boolean }) => void): () => void;
  connectRealtime(sdp: string): Promise<string>;
  getVoiceProviderName(): string;
  getVoiceProviderSelection(): VoiceProviderSelection;
  connectDoubaoVoice(): Promise<{ durationMs: number }>;
  sendDoubaoVoice(payload: string): void;
  closeDoubaoVoice(): void;
  onDoubaoVoiceMessage(callback: (payload: string) => void): () => void;
  onDoubaoVoiceState(callback: (state: DoubaoTransportState) => void): () => void;
  getVoiceSessionConfig(): Promise<import('./voice-provider').VoiceProviderSessionConfig>;
  executeAction(name: string, args: unknown): Promise<string>;
  quitApp(): void;
  getDesktopSettings(): Promise<DesktopSettings>;
  saveDesktopSettings(settings: Partial<DesktopSettings>): Promise<DesktopSettings>;
  onOpenPanel(callback: (panel: string) => void): () => void;
}

interface Window {
  jarvis: JarvisRendererApi;
}
