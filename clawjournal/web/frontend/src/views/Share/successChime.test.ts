import { beforeEach, describe, expect, it, vi } from 'vitest';

describe('success sound', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
  });

  it('preloads the bundled MP3 without transforming it', async () => {
    const sound = {
      currentTime: 0,
      loop: false,
      paused: true,
      preload: '',
      volume: 1,
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
    expect(sound.loop).toBe(true);
    expect(sound.volume).toBe(0);
    expect(sound.load).toHaveBeenCalledOnce();
    expect(sound.play).toHaveBeenCalledOnce();
  });

  it('restarts an armed element after delayed submission success', async () => {
    let resolvePrimingPlayback!: () => void;
    const sound = {
      currentTime: 0,
      loop: false,
      paused: true,
      preload: '',
      volume: 1,
      load: vi.fn(),
      pause: vi.fn(),
      play: vi.fn().mockImplementation(() => new Promise<void>((resolve) => {
        resolvePrimingPlayback = () => {
          sound.paused = false;
          resolve();
        };
      })),
    };
    vi.stubGlobal('Audio', vi.fn(function AudioMock() { return sound; }));

    const { playSuccessChime, primeSuccessChime } = await import('./successChime.ts');
    primeSuccessChime();
    playSuccessChime();

    expect(sound.play).toHaveBeenCalledOnce();
    expect(sound.currentTime).toBe(0);
    expect(sound.loop).toBe(false);
    expect(sound.volume).toBe(1);

    resolvePrimingPlayback();
    await Promise.resolve();

    expect(sound.play).toHaveBeenCalledOnce();
  });

  it('stops silent playback when submission does not succeed', async () => {
    const sound = {
      currentTime: 0,
      loop: false,
      paused: true,
      preload: '',
      volume: 1,
      load: vi.fn(),
      pause: vi.fn(),
      play: vi.fn().mockResolvedValue(undefined),
    };
    vi.stubGlobal('Audio', vi.fn(function AudioMock() { return sound; }));

    const { cancelSuccessChime, primeSuccessChime } = await import('./successChime.ts');
    primeSuccessChime();
    cancelSuccessChime();

    expect(sound.pause).toHaveBeenCalledOnce();
    expect(sound.currentTime).toBe(0);
    expect(sound.loop).toBe(false);
    expect(sound.volume).toBe(1);
  });
});
