from examples.sample_calc import average, clamp, parse_percent


def test_average():
    assert average([1, 2, 3]) == 2


def test_average_empty():
    assert average([]) == 0.0


def test_clamp():
    assert clamp(5, 0, 3) == 3
    assert clamp(-1, 0, 3) == 0


def test_parse_percent():
    assert parse_percent("42%") == 0.42
