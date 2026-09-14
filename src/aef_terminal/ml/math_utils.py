from __future__ import annotations

import numpy as np


def softmax_rows(values: np.ndarray) -> np.ndarray:
    """Return numerically stable row-wise probabilities for a 2D matrix."""

    shifted = values - values.max(axis=1, keepdims=True)
    exponentials = np.exp(shifted)
    return exponentials / exponentials.sum(axis=1, keepdims=True)
