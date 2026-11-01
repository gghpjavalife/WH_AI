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
    frame: pd.DataFrame,
    returns_column: str = "Returns_%",
    theme: str = "light",
    *,
    tint_rows: bool = True,
):
    """Apply return-aware styling to an investment table."""
    colors = _PERFORMANCE_COLORS.get(theme, _PERFORMANCE_COLORS["light"])
    neutral_background, neutral_foreground = colors["neutral"]

    def row_style(row: pd.Series) -> list[str]:
        if not tint_rows:
            return [
                f"background-color: {neutral_background}; color: {neutral_foreground};"
                for _ in row
            ]
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

    styled = frame.style.apply(row_style, axis=1)
    if not tint_rows:
        positive_text = "#16A34A" if theme == "light" else "#4ADE80"
        negative_text = "#DC2626" if theme == "light" else "#F87171"
        change_columns = [
            column
            for column in ("P&L", returns_column)
            if column in frame.columns
        ]

        def change_style(value: object) -> str:
            number = pd.to_numeric(value, errors="coerce")
            if pd.isna(number) or number == 0:
                return f"color: {neutral_foreground};"
            color = positive_text if number > 0 else negative_text
            return f"color: {color}; font-weight: 600;"

        if change_columns:
            styled = styled.map(change_style, subset=change_columns)
    if "Signal" in frame.columns:
        positive = "#216B44" if theme == "dark" else "#DCFCE7"
        negative = "#64282F" if theme == "dark" else "#FEE2E2"
        neutral = "#172334" if theme == "dark" else "#F1F5F9"
        text = "#F8FAFC" if theme == "dark" else "#172033"

        def signal_style(value: object) -> str:
            normalized = str(value).upper()
            if normalized in {"BUY", "STRONG BUY"}:
                background = positive
            elif normalized in {"SELL", "STRONG SELL"}:
                background = negative
            else:
                background = neutral
            return f"background-color: {background}; color: {text}"

        styled = styled.map(signal_style, subset=["Signal"])
    return styled
