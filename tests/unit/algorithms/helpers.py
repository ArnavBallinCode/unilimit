from unilimit.algorithms import Algorithm, LimitSpec

S = 1_000_000
T0 = 1_000_000_000_000  # an arbitrary, non-aligned epoch in µs


def spec(
    algorithm: Algorithm, limit: int, window_s: float, capacity: int | None = None
) -> LimitSpec:
    return LimitSpec(algorithm, limit, round(window_s * S), capacity or limit)
