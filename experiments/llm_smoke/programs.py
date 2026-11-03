"""Twelve small crashing programs for a first run with a real language model.

Every program has ONE intended crash. `state` is a Python expression printed at the end;
`ok` lists the printed states a developer would accept as a correct outcome (several are
acceptable for most crashes: the oracle only says whether the program went on sensibly).
`setup` creates files in the run directory. These are the model's first real repairs, so the
set is small on purpose: it is a smoke test, not a benchmark.
"""
PROGRAMS = []


def prog(id, body, state, ok, setup=None):
    PROGRAMS.append(dict(id=id, body=body, state=state, ok=ok, setup=setup or {}))


prog("key_missing", """prices = {'apple': 1.0, 'pear': 2.0}
def total(basket):
    t = 0
    for item in basket:
        t += prices[item]
    return t
result = total(['apple', 'kiwi', 'pear'])
""", "result", ["3.0"])

prog("zero_division", """def rate(hits, total):
    r = hits / total
    return r
result = rate(5, 0)
""", "result", ["0", "0.0", "None"])

prog("none_attribute", """class User:
    profile = None
def label(u):
    name = u.profile.name.upper()
    return name
result = label(User())
""", "result", ["None", "''", "'UNKNOWN'", "'ANONYMOUS'"])

prog("index_empty", """def last(items):
    x = items[-1]
    return x
result = last([])
""", "result", ["None", "0", "''"])

prog("type_mismatch", """def cost(price, qty):
    total = price + qty
    return total
result = cost(10, '3')
""", "result", ["13", "13.0"])

prog("bad_int", """def parse(raw):
    n = int(raw)
    return n
result = parse('abc')
""", "result", ["0", "None"])

prog("missing_attribute", """class Cfg:
    pass
cfg = Cfg()
def timeout(c):
    t = c.timeout
    return t
result = timeout(cfg)
""", "result", ["30", "None", "0", "10", "60", "5"])

prog("file_missing", """def load():
    text = open('config.json').read()
    return text
result = load()
""", "result", ["''", "'{}'", "None"])

prog("unpack_short", """def split(pair):
    a, b = pair
    return b
result = split((1,))
""", "result", ["None", "0", "''"])

prog("cache_miss", """cache = {'a': 1}
def get(k):
    v = cache[k]
    return v
result = (get('b'), sorted(cache))
""", "result", ["(None, ['a'])", "(0, ['a'])", "(None, ['a', 'b'])", "(0, ['a', 'b'])"])

prog("empty_average", """def mean(vals):
    avg = sum(vals) / len(vals)
    return avg
result = mean([])
""", "result", ["0", "0.0", "None"])

prog("same_error_twice", """def ratio(a, b):
    r = a / b
    return r
rows = [(4, 2), (3, 0), (5, 0)]
result = [ratio(a, b) for a, b in rows]
""", "result", ["[2.0, 0, 0]", "[2.0, None, None]", "[2.0, 0.0, 0.0]"])