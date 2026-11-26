"""Sample module for the pre-instrumentation tests (Python 3.7 syntax only)."""
import math

PRICES = {"a": 1, "b": 2}
BIG = 2 ** 70
COMPLEX = 3 + 4j
RAW = b"\x00\xff"
NOTHING = ...
INF = float("inf")
NAN_LIKE = 1e308 * 10


def slices(x):
    """Index, slice and extended slice forms."""
    return x[1], x[1:3], x[::2], x[1:3, 0] if False else x[0:1]


def table(items):
    total = 0
    for i in items:
        total += PRICES[i]
    return total


def ratio(x, y):
    r = x / y
    return r


def fstring(name, n):
    return f"{name!r:>8} has {n:03d} items, {n + 1} next"


def nested(n):
    def inner(k):
        return [i * k for i in range(n) if i % 2 == 0]
    squares = {i: i * i for i in range(n)}
    return inner(2), squares, (lambda a, *rest, **kw: (a, rest, kw))(1, 2, c=3)


def guarded(x):
    try:
        return 10 / x
    except ZeroDivisionError:
        return None


counter = 0


def bump():
    global counter
    counter += 1
    return counter


class Box:
    kind = "box"

    def __init__(self, v):
        self.v = v

    @property
    def twice(self):
        return self.v * 2

    @staticmethod
    def make(v):
        return Box(v)


async def acoro(v):
    return v + 1


def condition(x):
    if x > 0 and math.sqrt(x) < 10:
        return "small"
    elif x is None:
        return "none"
    return "big"