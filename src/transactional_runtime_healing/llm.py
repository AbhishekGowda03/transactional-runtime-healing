"""LLM plug-in: asks a language model for healing code at a crash.

    propose = LLMPropose()                     # settings from environment variables
    healer  = LiveHealer(propose)              # or PlainHealer(propose)

Any server with the OpenAI-style chat API works (Ollama, OpenAI, DeepSeek, OpenRouter, ...).
Nothing but the standard library is needed. Settings (environment variables; arguments of
LLMPropose override them):

    TXH_MODEL       model name                       default qwen2.5-coder:7b
    TXH_API_BASE    base URL of the API              default http://localhost:11434/v1 (Ollama)
    TXH_API_STYLE   openai | anthropic               default openai (Ollama, OpenAI, DeepSeek...)
    TXH_API_KEY     key, if the server wants one     default none (Ollama needs none)
    TXH_MAX_CALLS   hard cap on calls to the model   default 200 (per process; across
                    processes if TXH_BUDGET_FILE is set - the experiment runners do that)
    TXH_BUDGET_FILE file that counts real calls      default none (count per process)
    TXH_CACHE_DIR   where answers are cached         default results/llm_cache
    TXH_TEMPERATURE sampling temperature             default 0 (the same answer every time)
    TXH_RUN_ID      repeat number; part of the cache key and the seed, so repeated runs ask
                    again instead of replaying the first answer (use with a temperature > 0)
    TXH_PROMPT      rules | minimal                  default rules (see PROMPTS)
    TXH_MAX_TOKENS  limit on the answer length       default 400 (reasoning models also spend
                    it on thinking: use 2000 or more for them)
    TXH_TOKEN_PARAM name of that limit in the request default max_tokens (some newer OpenAI
                    models want max_completion_tokens)
    TXH_SEND_TEMPERATURE  1 | 0                      default 1; 0 leaves the temperature out
                    (models that only accept their default)
    TXH_EXTRA_BODY  JSON object merged into the request, e.g. {"reasoning_effort": "low"}

The same settings can be put in a file named `.env` (see `.env.example`): it is read from the
current folder or any folder above it, and real environment variables win over it. Only
TXH_* names are taken from it. `.env` is ignored by git; `.env.example` is committed.

The key is read from the environment (or .env) only. It is never written to the cache, the log or the
output.

Every answer is cached as one JSON file, keyed by the model, the temperature and the whole
prompt. A repeated request is answered from the cache at no cost, which is what lets the four
settings (plain, guard, protocol, combination) replay exactly the same repairs. The prompt is
built to be stable between runs (no memory addresses, no temporary paths), otherwise the
cache would never hit.

    replay_only=True   never call the model; a request that is not in the cache gets no repair
    dry_run=True       print the prompt and return no repair; nothing is called or cached

Command line helpers:
    python -m transactional_runtime_healing.llm --ping          one small request to the server
    python -m transactional_runtime_healing.llm --summary DIR   calls and tokens in a cache
"""
import argparse
import ast
import fcntl
import hashlib
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from typing import Optional

def load_dotenv(start=None) -> Optional[str]:
    """Read TXH_* settings from the nearest `.env` file (this folder or a parent). Values
    already in the environment are kept. Returns the path that was read, if any.
    TXH_IGNORE_DOTENV=1 turns it off (the test suite sets it, so a developer's own `.env`
    cannot change what the tests see)."""
    if os.environ.get("TXH_IGNORE_DOTENV"):
        return None
    d = os.path.abspath(start or os.getcwd())
    while True:
        path = os.path.join(d, ".env")
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("'\"")
                        if k.startswith("TXH_") and k not in os.environ:
                            os.environ[k] = v
            except OSError:
                return None
            return path
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


DEFAULT_MODEL = "qwen2.5-coder:7b"
DEFAULT_API_BASE = "http://localhost:11434/v1"
RERAISE = "RERAISE"

# The rule about what to assign depends on the failing statement. `_eh_rtn` is only mentioned
# for a `return`: in a first run the model wrote invented names like `_rh_rtn` for plain
# assignments, probably because the special name was in the prompt for every crash.
_TARGET_ASSIGN = """- Assign the variable(s) that the failing statement would have assigned, using exactly the \
names on the left side of the failing statement (for `r = a / b` assign `r`) and a sensible \
value."""
_TARGET_RETURN = """- The failing statement is a `return`. Assign the value that should be returned to the \
variable `_eh_rtn` and to nothing else."""
_TARGET_COND = """- The failing statement is the condition of an `if`. Assign the truth value the condition \
should have (True or False) to the variable `_eh_cond` and to nothing else. The program then \
runs the matching branch of the `if`."""
_SAFETY_RULE = """- Keep the code minimal. Do not print. Do not read or write files, start processes, use the \
network, or change global settings. Do not modify objects that are shared with the rest of the \
program unless that is the only way to repair the failure."""
_HEAD = """You repair a Python program at the moment it crashes. The program is paused \
at the failing statement. You write a short piece of Python code that is run in the \
namespace of the crashed function (its local variables and the module globals are available). \
After your code runs, the program continues with the statement AFTER the failing one; the \
failing statement is not run again."""
_TAIL = f"""- If you cannot repair it safely, answer with exactly {RERAISE}.
Answer with one Python code block and nothing else."""

# The prompt is an experimental variable. "rules" tells the model what not to touch; "minimal"
# does not, so the two can be compared (do the safety rules, not the protocol, keep repairs
# harmless?). HealBench's own prompt should replace both once it is available.
PROMPTS = {
    "rules": f"{_HEAD}\n\nRules:\n{{target}}\n{_SAFETY_RULE}\n{_TAIL}",
    "minimal": f"{_HEAD}\n\nRules:\n{{target}}\n{_TAIL}",
}


def system_prompt(name: str, statement: str) -> str:
    """The system message for a prompt variant and the failing statement."""
    st = statement.lstrip()
    if st.startswith("return"):
        target = _TARGET_RETURN
    elif st.startswith("if ") and st.rstrip().endswith(":"):
        target = _TARGET_COND
    else:
        target = _TARGET_ASSIGN
    return PROMPTS[name].replace("{target}", target)


SYSTEM_PROMPT = system_prompt("rules", "")


class LLMError(Exception):
    pass


def _stable(text: str) -> str:
    """Remove what changes between runs but means nothing (memory addresses, temp paths)."""
    text = re.sub(r"0x[0-9a-fA-F]{4,}", "0x…", text)
    text = re.sub(r"/tmp/[^\s'\"]+", "/tmp/…", text)
    return text


def _short(value, limit=200) -> str:
    try:
        r = repr(value)
    except Exception:
        r = f"<{type(value).__name__}>"
    r = _stable(r)
    return r if len(r) <= limit else r[:limit] + "…"


def _function_source(filename: str, lineno: int, max_lines: int = 60) -> str:
    """Source of the function that contains the line (or the lines around it)."""
    try:
        with open(filename, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
        best = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    node.lineno <= lineno <= (node.end_lineno or node.lineno):
                if best is None or node.lineno >= best.lineno:
                    best = node
        if best is not None:
            return "\n".join(lines[best.lineno - 1:best.end_lineno][:max_lines])
    except SyntaxError:
        pass
    lo = max(lineno - 8, 0)
    return "\n".join(lines[lo:lineno + 8])


def build_prompt(ctx) -> str:
    """The user message: what the model is told about the crash (stable between runs)."""
    exc = ctx.exception
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-6:])
    tb = _stable(re.sub(r'File "([^"]*[/\\])?([^"/\\]+)"', r'File "\2"', tb))
    names = [n for n in ctx.locals_ if not n.startswith("__") and n not in ("_eh_rtn", "_eh_cond")][:30]
    local_lines = "\n".join(f"  {n} = {_short(ctx.locals_[n])}  # {type(ctx.locals_[n]).__name__}"
                            for n in names) or "  (none)"
    src = _function_source(ctx.filename, ctx.lineno)
    base = os.path.basename(ctx.filename)
    return (f"File: {base}, line {ctx.lineno}, in {ctx.function}\n"
            f"Exception: {type(exc).__name__}: {_stable(str(exc))[:300]}\n\n"
            f"Failing statement:\n{ctx.statement.strip()}\n\n"
            f"Enclosing function:\n{src}\n\n"
            f"Local variables at the crash:\n{local_lines}\n\n"
            f"Traceback (last frames):\n{tb}")


def classify_reply(reply: str):
    """(code, kind) for a model reply. kind is one of
         ok         `code` is the healing code (a closed code block, or a reply that is code)
         decline    the model answered RERAISE
         truncated  a code block that was opened and never closed (the answer was cut off)
         invalid    anything else (an apology, an explanation: no code)
    Only `ok` replies are run. A reply that is prose is never executed as code."""
    reply = re.sub(r"<think>.*?</think>", "", reply or "", flags=re.S).strip()
    if not reply:
        return None, "invalid"
    m = re.search(r"```(?:python|py)?[ \t]*\n(.*?)```", reply, flags=re.S)
    if m:
        code = m.group(1).strip("\n")
        if re.fullmatch(r"\W*" + RERAISE + r"\W*", code.strip(), flags=re.I):
            return None, "decline"
        return (code, "ok") if code.strip() else (None, "invalid")
    if "```" in reply:
        return None, "truncated"
    if re.fullmatch(r"\W*" + RERAISE + r"\W*", reply, flags=re.I):
        return None, "decline"
    try:                                       # unfenced reply: only if it really is code
        tree = ast.parse(reply)
    except SyntaxError:
        return None, "invalid"
    if all(isinstance(n, ast.Expr) and isinstance(n.value, (ast.Name, ast.Constant))
           for n in tree.body):
        return None, "invalid"
    return reply, "ok"


def extract_code(reply: str) -> Optional[str]:
    """The healing code in a model reply, or None (declined, cut off, or no code)."""
    return classify_reply(reply)[0]


class LLMPropose:
    """propose(context) -> healing code or None. Callable; give it to a healer."""

    def __init__(self, model=None, api_base=None, api_key=None, cache_dir=None,
                 temperature=None, max_tokens=None, max_calls=None,
                 replay_only: bool = False, dry_run: bool = False, timeout: float = 60.0,
                 run_id=None, prompt=None, budget_file=None, max_consecutive_errors: int = 3, events_path=None):
        load_dotenv()
        self.model = model or os.environ.get("TXH_MODEL", DEFAULT_MODEL)
        self.style = os.environ.get("TXH_API_STYLE", "openai").lower()
        if self.style not in ("openai", "anthropic"):
            raise ValueError("TXH_API_STYLE must be openai or anthropic")
        self.api_base = (api_base or os.environ.get("TXH_API_BASE", DEFAULT_API_BASE)).rstrip("/")
        self._key = api_key or os.environ.get("TXH_API_KEY", "")
        self.cache_dir = cache_dir or os.environ.get("TXH_CACHE_DIR", "results/llm_cache")
        self.temperature = float(temperature if temperature is not None
                                 else os.environ.get("TXH_TEMPERATURE", 0.0))
        self.run_id = str(run_id if run_id is not None else os.environ.get("TXH_RUN_ID", ""))
        self.prompt_name = prompt or os.environ.get("TXH_PROMPT", "rules")
        if self.prompt_name not in PROMPTS:
            raise ValueError("TXH_PROMPT must be one of: " + ", ".join(PROMPTS))
        self.budget_file = budget_file or os.environ.get("TXH_BUDGET_FILE", "")
        self.max_consecutive_errors = max_consecutive_errors
        self._consecutive_errors = 0
        self.events_path = events_path
        self.max_tokens = int(max_tokens if max_tokens is not None
                              else os.environ.get("TXH_MAX_TOKENS", 400))
        self.token_param = os.environ.get("TXH_TOKEN_PARAM", "max_tokens")
        self.send_temperature = os.environ.get("TXH_SEND_TEMPERATURE", "1") != "0"
        try:
            self.extra_body = json.loads(os.environ.get("TXH_EXTRA_BODY") or "{}")
            if not isinstance(self.extra_body, dict):
                raise ValueError
        except ValueError:
            raise ValueError("TXH_EXTRA_BODY must be a JSON object") from None
        self.max_calls = int(max_calls if max_calls is not None
                             else os.environ.get("TXH_MAX_CALLS", 200))
        self.replay_only = replay_only
        self.dry_run = dry_run
        self.timeout = timeout
        self.stats = dict(calls=0, cache_hits=0, misses=0, declined=0, invalid=0, truncated=0,
                          errors=0, unavailable=False, prompt_tokens=0, completion_tokens=0,
                          budget_exhausted=False, model=self.model, run_id=self.run_id,
                          prompt=self.prompt_name, temperature=self.temperature)
        self.last_error = ""

    # -- cache ------------------------------------------------------------------------
    def _key_for(self, messages) -> str:
        d = dict(model=self.model, temperature=self.temperature,
                 max_tokens=self.max_tokens, messages=messages)
        if self.extra_body or not self.send_temperature or self.token_param != "max_tokens":
            d["request_options"] = dict(extra=self.extra_body, send_temperature=self.send_temperature,
                                        token_param=self.token_param)   # only when non-default
        if self.run_id:                       # only when set, so run-less caches stay valid
            d["run_id"] = self.run_id
        blob = json.dumps(d, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    def _path(self, key) -> str:
        return os.path.join(self.cache_dir, f"{key}.json")

    def _load(self, key):
        try:
            with open(self._path(key), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _store(self, key, record):
        os.makedirs(self.cache_dir, exist_ok=True)
        tmp = self._path(key) + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=1)
        os.replace(tmp, self._path(key))

    # -- the call ---------------------------------------------------------------------
    def _chat(self, messages):
        """One request, retried a few times when the failure is transient (network error,
        timeout, HTTP 429 or 5xx): an unreachable model must not look like 'no repair'."""
        delays = [float(x) for x in os.environ.get("TXH_RETRY_DELAYS", "2,6,15").split(",") if x.strip()]
        for delay in [*delays, None]:
            try:
                return self._chat_once(messages)
            except LLMError as e:
                if delay is None or not getattr(e, "transient", False):
                    raise
                time.sleep(delay)

    def _chat_once(self, messages):
        headers = {"Content-Type": "application/json"}
        if self.style == "anthropic":                      # Anthropic Messages API
            system = "\n".join(m["content"] for m in messages if m["role"] == "system")
            rest = [m for m in messages if m["role"] != "system"]
            body = json.dumps(dict(model=self.model, system=system, messages=rest,
                                   temperature=self.temperature,
                                   max_tokens=self.max_tokens)).encode()
            headers["x-api-key"] = self._key
            headers["anthropic-version"] = "2023-06-01"
            url = self.api_base + "/messages"
        else:                                              # OpenAI-style chat API
            req_body = dict(model=self.model, messages=messages, stream=False)
            req_body[self.token_param] = self.max_tokens
            if self.send_temperature:
                req_body["temperature"] = self.temperature
            req_body.update(self.extra_body)
            if self.run_id:                   # a different seed for every repeat
                req_body["seed"] = int(hashlib.sha256(self.run_id.encode()).hexdigest()[:7], 16)
            body = json.dumps(req_body).encode()
            if self._key:
                headers["Authorization"] = f"Bearer {self._key}"
            url = self.api_base + "/chat/completions"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = ""
            if e.code not in (401, 403):          # the message of a rejected request says which
                try:                              # setting was wrong; never shown for key errors
                    raw = json.loads(e.read().decode()[:2000])
                    err = raw.get("error", raw) if isinstance(raw, dict) else {}
                    detail = ": " + str(err.get("message", err) if isinstance(err, dict) else err)[:300]
                except Exception:
                    pass
            err = LLMError(f"HTTP {e.code} from {self.api_base}{detail}")
            err.transient = e.code == 429 or e.code >= 500
            raise err from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            err = LLMError(f"cannot reach {self.api_base}: {type(e).__name__}")
            err.transient = True
            raise err from None
        try:
            if self.style == "anthropic":
                reply = "".join(b.get("text", "") for b in data["content"])
                u = data.get("usage") or {}
                usage = dict(prompt_tokens=u.get("input_tokens", 0),
                             completion_tokens=u.get("output_tokens", 0))
            else:
                reply = data["choices"][0]["message"]["content"] or ""
                usage = data.get("usage") or {}
        except (KeyError, IndexError, TypeError):
            raise LLMError("unexpected answer format") from None
        return reply, usage

    def _spend(self) -> bool:
        """Count one real call against the cap. With a budget file the count is shared by every
        process that uses the file (the runners start one process per program)."""
        if not self.budget_file:
            return self.stats["calls"] < self.max_calls
        os.makedirs(os.path.dirname(os.path.abspath(self.budget_file)), exist_ok=True)
        with open(self.budget_file, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            used = int(f.read().strip() or 0)
            if used >= self.max_calls:
                return False
            f.seek(0)
            f.truncate()
            f.write(str(used + 1))
            return True

    def _event(self, **kw):
        """Append one line per request to the events file. Appending (not one summary at the
        end) keeps the record complete across forks: a trial child that is killed, or the
        parent that goes on, each write their own lines."""
        if not self.events_path:
            return
        fd = os.open(self.events_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(dict(kw, pid=os.getpid())) + "\n").encode())
        finally:
            os.close(fd)

    def __call__(self, ctx) -> Optional[str]:
        messages = [dict(role="system", content=system_prompt(self.prompt_name, ctx.statement)),
                    dict(role="user", content=build_prompt(ctx))]
        if self.dry_run:
            print("----- prompt (dry run, nothing sent) -----\n" + messages[1]["content"]
                  + "\n-----", file=sys.stderr)
            return None
        key = self._key_for(messages)
        record = self._load(key)
        from_cache = record is not None
        if record is not None:
            self.stats["cache_hits"] += 1
        else:
            if self.replay_only:
                self.stats["misses"] += 1
                self._event(source="none", outcome="cache_miss")
                return None
            if self._consecutive_errors >= self.max_consecutive_errors:
                self.stats["unavailable"] = True         # the model cannot be reached: stop
                self._event(source="none", outcome="unavailable")
                return None                              # waiting for it at every crash
            if not self._spend():
                self.stats["budget_exhausted"] = True
                self._event(source="none", outcome="budget_exhausted")
                return None
            self.stats["calls"] += 1
            t0 = time.monotonic()
            try:
                reply, usage = self._chat(messages)
            except LLMError as e:
                self.stats["errors"] += 1
                self._consecutive_errors += 1
                self.last_error = str(e)
                print(f"[llm] {e}", file=sys.stderr)
                self._event(source="model", outcome="error", error=str(e))
                return None
            self._consecutive_errors = 0
            record = dict(model=self.model, temperature=self.temperature, run_id=self.run_id,
                          messages=messages, reply=reply, usage=usage,
                          seconds=round(time.monotonic() - t0, 2),
                          created=time.strftime("%Y-%m-%dT%H:%M:%S"))
            self._store(key, record)
        usage = record.get("usage") or {}
        self.stats["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.stats["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        code, kind = classify_reply(record["reply"])
        self._event(source="cache" if from_cache else "model", outcome=kind,
                    seconds=record.get("seconds"))
        if kind == "decline":
            self.stats["declined"] += 1
        elif kind in ("invalid", "truncated"):
            self.stats[kind] += 1
        return code


def summarize(cache_dir: str) -> dict:
    out = dict(files=0, prompt_tokens=0, completion_tokens=0, seconds=0.0, models=set())
    for name in os.listdir(cache_dir):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(cache_dir, name), encoding="utf-8") as f:
                r = json.load(f)
        except (OSError, ValueError):
            continue
        out["files"] += 1
        u = r.get("usage") or {}
        out["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        out["completion_tokens"] += int(u.get("completion_tokens") or 0)
        out["seconds"] += float(r.get("seconds") or 0)
        out["models"].add(r.get("model"))
    out["models"] = sorted(m for m in out["models"] if m)
    out["seconds"] = round(out["seconds"], 1)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ping", action="store_true", help="send one small request")
    ap.add_argument("--summary", metavar="DIR", help="summarise a cache directory")
    ap.add_argument("--model")
    ap.add_argument("--api-base")
    ns = ap.parse_args(argv)
    if ns.summary:
        print(json.dumps(summarize(ns.summary), indent=1))
        return 0
    if ns.ping:
        p = LLMPropose(model=ns.model, api_base=ns.api_base)
        print(f"model {p.model} at {p.api_base} ({p.style} style)")
        try:
            reply, usage = p._chat([dict(role="user", content="Reply with the single word: ok")])
        except LLMError as e:
            print(f"FAILED: {e}")
            return 1
        print(f"answer: {reply.strip()[:60]!r}  tokens: {usage}")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())