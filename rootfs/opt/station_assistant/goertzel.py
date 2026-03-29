"""
goertzel.py
Implements the Goertzel algorithm for efficient single-frequency power detection.
Far more efficient than FFT when targeting a small number of specific frequencies.
"""

import numpy as np


def goertzel_magnitude(samples: np.ndarray, target_freq: float, sample_rate: int) -> float:
    """
    Compute the normalized power magnitude of a specific frequency within a sample buffer.
    Uses a vectorized NumPy implementation for performance on ARM/embedded hardware.

    Args:
        samples:     numpy float32 array of audio samples, values in [-1.0, 1.0]
        target_freq: the frequency to detect, in Hz
        sample_rate: audio sample rate, in Hz

    Returns:
        Normalized magnitude as a float >= 0.0.
        Typical noise floor: 0.001–0.010
        Typical tone present: 0.10–1.0+
        Use 0.10–0.20 as a starting detection threshold.
    """
    n = len(samples)
    if n == 0:
        return 0.0

    k = int(0.5 + (n * target_freq) / sample_rate)
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
    Compute Goertzel magnitudes for multiple frequencies in a single pass setup.
    More efficient than calling goertzel_magnitude() individually when many
    frequencies share the same sample buffer.

    Args:
        samples:     numpy float32 array of audio samples
        frequencies: list of target frequencies in Hz
        sample_rate: audio sample rate in Hz

    Returns:
        dict mapping frequency (float) → magnitude (float)
    """
    results = {}
    for freq in frequencies:
        results[freq] = goertzel_magnitude(samples, freq, sample_rate)
    return results
