"""
goertzel.py
Implements the Goertzel algorithm for efficient single-frequency power detection.
Far more efficient than FFT when targeting a small number of specific frequencies.
"""

import numpy as np

# Frequency window half-width in Hz.  For each target frequency we also
# test at ±FREQ_WINDOW_HZ offsets and return the best magnitude.  This
# compensates for slight encoder drift or analog path shifts.
FREQ_WINDOW_HZ = 6.0
FREQ_WINDOW_STEPS = 3  # number of offsets each side (total probes = 2*STEPS + 1)


def goertzel_magnitude(samples: np.ndarray, target_freq: float, sample_rate: int) -> float:
    """
    Compute the normalized power magnitude of a specific frequency within a sample buffer.

    Uses fractional-k Goertzel so the filter is tuned to the exact target
    frequency rather than the nearest FFT bin.  This eliminates spectral
    leakage errors that occur when the target falls between bins.

    Args:
        samples:     numpy float32 array of audio samples, values in [-1.0, 1.0]
        target_freq: the frequency to detect, in Hz
        sample_rate: audio sample rate, in Hz

    Returns:
        Normalized magnitude as a float >= 0.0.
        Typical noise floor: 0.001–0.010
        Typical tone present: 0.01–0.25 (scales with signal amplitude squared;
            a full-scale sine wave in [-1, 1] produces a maximum of ~0.25)
        Use 0.05–0.15 as a starting detection threshold and lower only if
        tones are genuinely weak at the audio input.
    """
    n = len(samples)
    if n == 0:
        return 0.0

    # Fractional k — tune to the exact target frequency, not the nearest bin.
    k = (n * target_freq) / sample_rate
    omega = (2.0 * np.pi * k) / n
    coeff = 2.0 * np.cos(omega)

    # Vectorized Goertzel: process all samples via cumulative recurrence
    # s[i] = samples[i] + coeff * s[i-1] - s[i-2]
    # We must iterate since each step depends on the previous two values,
    # but we do it with a pre-allocated array and minimal Python overhead.
    s = np.empty(n + 2, dtype=np.float64)
    s[0] = 0.0
    s[1] = 0.0
    samp = samples.astype(np.float64)
    for i in range(n):
        s[i + 2] = samp[i] + coeff * s[i + 1] - s[i]

    s_prev = s[n + 1]
    s_prev2 = s[n]
    power = s_prev2 ** 2 + s_prev ** 2 - coeff * s_prev * s_prev2
    # Normalize: divide by n^2 so magnitude is independent of buffer size
    magnitude = max(0.0, power) / (n * n)
    return magnitude


def rms_level(samples: np.ndarray) -> float:
    """
    Compute the RMS (Root Mean Square) audio level of a sample buffer.

    Returns:
        Float in [0.0, 1.0] representing signal level.
    """
    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


def batch_goertzel(samples: np.ndarray, frequencies: list, sample_rate: int) -> dict:
    """
    Compute Goertzel magnitudes for multiple frequencies with a frequency
    window.  For each target frequency, probes at small offsets around the
    center and returns the best (highest) magnitude.  This compensates for
    slight frequency drift in the paging encoder.

    Args:
        samples:     numpy float32 array of audio samples
        frequencies: list of target frequencies in Hz
        sample_rate: audio sample rate in Hz

    Returns:
        dict mapping frequency (float) → best magnitude (float)
    """
    results = {}
    # Pre-compute the offset list once
    if FREQ_WINDOW_STEPS > 0 and FREQ_WINDOW_HZ > 0:
        step = FREQ_WINDOW_HZ / FREQ_WINDOW_STEPS
        offsets = [i * step for i in range(-FREQ_WINDOW_STEPS, FREQ_WINDOW_STEPS + 1)]
    else:
        offsets = [0.0]

    for freq in frequencies:
        best = 0.0
        for offset in offsets:
            mag = goertzel_magnitude(samples, freq + offset, sample_rate)
            if mag > best:
                best = mag
        results[freq] = best
    return results
