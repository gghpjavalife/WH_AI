"""Shared return-based styling for investment tables."""

import pandas as pd


_PERFORMANCE_COLORS = {
    "light": {
        "strong_gain": ("#BCE7C7", "#14532D"),
        "gain": ("#D8F3DF", "#14532D"),
        "small_gain": ("#EAF7EE", "#14532D"),
        "neutral": ("#F1F5F9", "#475569"),
        "small_loss": ("#FCEBEA", "#7F1D1D"),
        "loss": ("#FBD5D2", "#7F1D1D"),
        "strong_loss": ("#F4B8B3", "#7F1D1D"),
    },
    "dark": {
        "strong_gain": ("#216B44", "#ECFDF5"),
        "gain": ("#1B5237", "#D1FAE5"),
        "small_gain": ("#173D2A", "#D1FAE5"),
        "neutral": ("#172334", "#CBD5E1"),
        "small_loss": ("#4B2429", "#FEE2E2"),
        "loss": ("#64282F", "#FEE2E2"),
        "strong_loss": ("#7C2D32", "#FFF1F2"),
    },
}


def style_returns(
    frame: pd.DataFrame, returns_column: str = "Returns_%", theme: str = "light"
):
    """Apply a readable, tiered background tint across each row by return."""
    colors = _PERFORMANCE_COLORS.get(theme, _PERFORMANCE_COLORS["light"])

    def row_style(row: pd.Series) -> list[str]:
        value = pd.to_numeric(row[returns_column], errors="coerce")
        if pd.isna(value) or value == 0:
            category = "neutral"
        elif value >= 20:
            category = "strong_gain"
        elif value > 5:
            category = "gain"
        elif value > 0:
            category = "small_gain"
        elif value <= -20:
            category = "strong_loss"
        elif value < -5:
            category = "loss"
        else:
            category = "small_loss"
        background, foreground = colors[category]
        return [
            f"background-color: {background}; color: {foreground};"
            for _ in row
        ]

    return frame.style.apply(row_style, axis=1)
