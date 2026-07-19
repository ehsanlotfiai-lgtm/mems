"""Scalping-specific strategies — optimized for 1m/5m timeframes on high-volume coins.

Each strategy targets quick, high-probability entries with tight SL/TP.
Strategies are designed for rapid profit-taking (1-3 minutes hold time).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict, Any

import numpy as np
import pandas as pd

from core.models import StrategyHit
from strategies import indicators as ind


@dataclass