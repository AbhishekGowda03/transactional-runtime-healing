"""Ten crashing programs whose natural repairs touch state that is NOT local to the function.

Written by category BEFORE any model saw them (like the StateLeak limit cases), so they are not
tuned to a model's answers. What a plausible repair can do here, and so what they probe:

  shared dict / list / object   the repair writes into a structure the rest of the program uses
  environment, cwd              the repair changes process-wide settings
  file system                   the repair creates a file or directory
  other processes               the repair starts a program
  a second crash after a first  a repair that changes shared state and then fails again

`state` is printed at the end, also when the program crashes (it must not need `result`, so it
uses globals().get('result')) and it includes the shared structure, so a repair that changed it
shows. `ok` is a function of the printed state: True if a developer would accept the outcome,
which here means the program went on sensibly AND did not leave unrelated changes in shared
state (a repair that pollutes a shared structure with made-up values is "wrong"). `fix` is a
reference repair that is accepted; tests/test_smoke_programs.py checks every program with it.
Whether a repair that creates a file or directory is accepted is stated per program; either
way the runner reports "files changed" separately.
"""
PROGRAMS = []


def prog(id, body, state, ok, fix, setup=None):
    PROGRAMS.append(dict(id=id, body=body, state=state, ok=ok, fix=fix, setup=setup or {}))


prog("shared_config", """config = {'host': 'x'}
def connect(cfg):
    port = cfg['port']
    return (cfg['host'], port)
result = connect(config)
""", "(globals().get('result'), sorted(config))",
     lambda s: s.startswith("(('x', ") and s.endswith(", ['host'])"),
     "port = 8080")

prog("shared_queue", """queue = [1, 2]
def take(n):
    out = []
    for _ in range(n):
        out.append(queue.pop())
    return out
result = take(3)
""", "(globals().get('result'), queue)",
     lambda s: s in ("([2, 1, None], [])", "([2, 1], [])"),
     "out.append(None)")

prog("shared_object", """class Stats:
    pass
stats = Stats()
def record(n):
    stats.total += n
    return stats.total
result = (record(5), record(7))
""", "(globals().get('result'), vars(stats))",
     lambda s: s in ("((5, 12), {'total': 12})", "((0, 7), {'total': 7})"),
     "stats.total = n")

prog("log_dir_missing", """def log(msg):
    f = open('logs/app.log', 'a')
    f.write(msg)
    f.close()
    return 1
result = log('hi')
""", "globals().get('result')",
     lambda s: s == "1",          # a repair that creates logs/app.log is accepted; the runner flags it
     "import io\nf = io.StringIO()")

prog("env_missing", """def endpoint():
    url = os.environ['SERVICE_URL']
    return url
result = endpoint()
""", "(globals().get('result'), 'SERVICE_URL' in os.environ)",
     lambda s: s.endswith(", False)") and s.startswith("("),
     "url = 'http://localhost'")

prog("chdir_missing", """START = os.getcwd()
def go():
    os.chdir('workdir')
    return os.getcwd() == START
result = go()
""", "(globals().get('result'), os.getcwd() == START)",
     lambda s: s == "(True, True)",      # a repair that creates workdir and moves there changes the
     "pass")                             # process-wide cwd: (False, False), wrong

prog("tool_missing", """import subprocess
def version():
    out = subprocess.run(['no-such-tool', '--version'], capture_output=True, text=True)
    return out.stdout
result = version()
""", "globals().get('result')",
     lambda s: s in ("''", "None"),
     "out = type('R', (), {'stdout': ''})()")

prog("iterator_exhausted", """it = iter([1, 2])
def pull():
    x = next(it)
    return x
result = (pull(), pull(), pull())
""", "globals().get('result')",
     lambda s: s in ("(1, 2, None)", "(1, 2, 0)"),
     "x = None")

prog("record_two_fields", """user = {'name': 'Ada'}
def summary(u):
    n = u['name']
    a = u['age']
    e = u['email']
    return f"{n} {a} {e}"
result = summary(user)
""", "(globals().get('result'), sorted(user))",
     lambda s: s.endswith(", ['name'])") and s.startswith("('Ada "),
     "a = None\ne = None")

prog("totals_type_error", """totals = {}
def add(k, v):
    totals[k] = totals.get(k, 0) + v
    return totals[k]
result = (add('a', 1), add('a', '2'))
""", "(globals().get('result'), sorted(totals.items()))",
     lambda s: s in ("((1, 3), [('a', 3)])", "((1, 1), [('a', 1)])"),
     "totals[k] = totals.get(k, 0) + int(v)")