const SUCCESS_SOUND_URL = '/sounds/submission-success.mp3';

let successSound: HTMLAudioElement | null = null;

function getSuccessSound(): HTMLAudioElement | null {
  if (typeof Audio === 'undefined') return null;
  if (!successSound) {
    successSound = new Audio(SUCCESS_SOUND_URL);
    successSound.preload = 'auto';
  }
  return successSound;
}

/** Begin loading the exact success MP3 while the upload is in progress. */
export function primeSuccessChime() {
  try {
    getSuccessSound()?.load();
  } catch {
    // Sound is optional; submission must continue if media is unavailable.
  }
}

/** Play the bundled success MP3 from the beginning without transforming it. */
export function playSuccessChime() {
  try {
    const sound = getSuccessSound();
    if (!sound) return;
    if (!sound.paused) sound.pause();
    sound.currentTime = 0;
    void sound.play().catch(() => {
      // Sound is optional; submission has already succeeded.
    });
  } catch {
    // Sound is optional; submission has already succeeded.
  }
}
