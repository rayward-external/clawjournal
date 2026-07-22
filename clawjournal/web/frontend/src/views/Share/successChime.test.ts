import { beforeEach, describe, expect, it, vi } from 'vitest';

describe('success sound', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
  });

  it('preloads the bundled MP3 without transforming it', async () => {
    const sound = {
      currentTime: 0,
      paused: true,
      preload: '',
      load: vi.fn(),
      pause: vi.fn(),
      play: vi.fn().mockResolvedValue(undefined),
    };
    const AudioConstructor = vi.fn(function AudioMock() { return sound; });
    vi.stubGlobal('Audio', AudioConstructor);

    const { primeSuccessChime } = await import('./successChime.ts');
    primeSuccessChime();

    expect(AudioConstructor).toHaveBeenCalledWith('/sounds/submission-success.mp3');
    expect(sound.preload).toBe('auto');
    expect(sound.load).toHaveBeenCalledOnce();
  });

  it('restarts and plays the exact asset', async () => {
    const sound = {
      currentTime: 6,
      paused: false,
      preload: '',
      load: vi.fn(),
      pause: vi.fn(),
      play: vi.fn().mockResolvedValue(undefined),
    };
    vi.stubGlobal('Audio', vi.fn(function AudioMock() { return sound; }));

    const { playSuccessChime } = await import('./successChime.ts');
    playSuccessChime();

    expect(sound.pause).toHaveBeenCalledOnce();
    expect(sound.currentTime).toBe(0);
    expect(sound.play).toHaveBeenCalledOnce();
  });
});
