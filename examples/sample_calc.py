"""Sample module used only to exercise Autopilot on a throwaway PR."""


def average(values):
    """Return the arithmetic mean of a list of numbers."""
    total = 0
    for v in values:
        total += v
    return total / len(values)


def clamp(x, low, high):
    """Clamp x into the closed range [low, high]."""
    if x < low:
        return low
    if x > high:
        return high
    return x


def parse_percent(text):
    """Parse a string like '42%' into the float 0.42."""
    return float(text.strip("%")) / 100
