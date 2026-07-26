const SUCCESS_SOUND_URL = '/sounds/submission-success.mp3';
const SUCCESS_CHIME_VOLUME = 0.25;
const SUCCESS_CHIME_DURATION_MS = 900;

let successSound: HTMLAudioElement | null = null;
let primingPlayback: Promise<boolean> | null = null;
let stopPlaybackTimer: ReturnType<typeof setTimeout> | null = null;

function getSuccessSound(): HTMLAudioElement | null {
  if (typeof Audio === 'undefined') return null;
  if (!successSound) {
    successSound = new Audio(SUCCESS_SOUND_URL);
    successSound.preload = 'auto';
  }
  return successSound;
}

function requestPlayback(sound: HTMLAudioElement): Promise<boolean> {
  try {
    return sound.play().then(
      () => true,
      () => false,
    );
  } catch {
    return Promise.resolve(false);
  }
}

function clearPlaybackStop() {
  if (stopPlaybackTimer === null) return;
  clearTimeout(stopPlaybackTimer);
  stopPlaybackTimer = null;
}

function schedulePlaybackStop(sound: HTMLAudioElement) {
  clearPlaybackStop();
  stopPlaybackTimer = setTimeout(() => {
    stopPlaybackTimer = null;
    try {
      sound.pause();
      sound.currentTime = 0;
    } catch {
      // Sound is optional; cleanup must not affect the completed submission.
    }
  }, SUCCESS_CHIME_DURATION_MS);
}

/**
 * Begin loading the exact success MP3 while the upload is in progress.
 *
 * Safari requires play() to be called directly from the user's click. Start
 * the reusable element silently here and keep it running until submission
 * succeeds, then restart it audibly in playSuccessChime().
 */
export function primeSuccessChime() {
  try {
    const sound = getSuccessSound();
    if (!sound) return;
    clearPlaybackStop();
    if (!sound.paused) sound.pause();
    sound.currentTime = 0;
    sound.loop = true;
    sound.volume = 0;
    sound.load();
    primingPlayback = requestPlayback(sound);
  } catch {
    primingPlayback = null;
    // Sound is optional; submission must continue if media is unavailable.
  }
}

/** Play a short, restrained excerpt of the bundled success MP3. */
export function playSuccessChime() {
  try {
    const sound = getSuccessSound();
    if (!sound) return;
    const primed = primingPlayback;
    primingPlayback = null;
    sound.loop = false;
    sound.currentTime = 0;
    sound.volume = SUCCESS_CHIME_VOLUME;
    schedulePlaybackStop(sound);

    // A normal submission is already playing the element silently, so changing
    // its time and volume does not need a fresh autoplay permission. Preserve a
    // direct-call fallback for callers that did not prime it first.
    if (!primed) {
      void requestPlayback(sound);
    } else if (sound.paused) {
      void primed.then((started) => {
        if (!started || sound.paused) void requestPlayback(sound);
      });
    }
  } catch {
    // Sound is optional; submission has already succeeded.
  }
}

/** Stop the silent priming loop when submission does not succeed. */
export function cancelSuccessChime() {
  primingPlayback = null;
  clearPlaybackStop();
  try {
    if (!successSound) return;
    successSound.pause();
    successSound.loop = false;
    successSound.currentTime = 0;
    successSound.volume = SUCCESS_CHIME_VOLUME;
  } catch {
    // Sound is optional; failure cleanup must not mask the submission error.
  }
}
