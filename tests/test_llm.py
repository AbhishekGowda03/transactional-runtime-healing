"""LLM plug-in tests, against a fake server that speaks the OpenAI-style chat API."""
import json
import os
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from transactional_runtime_healing import llm
from transactional_runtime_healing.instrument import HealContext

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


class Fake:
    """A fake model server. `replies` is a list of answers (the last one repeats)."""

    def __init__(self, replies):
        self.replies, self.requests, self.auth = list(replies), [], []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                outer.auth.append(self.headers.get("Authorization"))
                i = min(len(outer.requests) - 1, len(outer.replies) - 1)
                out = json.dumps(dict(choices=[dict(message=dict(content=outer.replies[i]))],
                                      usage=dict(prompt_tokens=11, completion_tokens=5))).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass
        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake():
    made = []

    def make(*replies):
        f = Fake(replies or ["```python\nv = 0\n```"])
        made.append(f)
        return f
    yield make
    for f in made:
        f.close()


def ctx(tmp_path, statement="v = 1 / 0"):
    src = tmp_path / "prog.py"
    src.write_text("def f():\n    v = 1 / 0\n    return v\n")
    try:
        1 / 0
    except ZeroDivisionError as e:
        exc = e
    return HealContext(exc, str(src), 2, "f", statement, {"x": [1, 2], "o": object()}, {})


def test_extract_code():
    assert llm.extract_code("```python\nv = 0\n```") == "v = 0"
    assert llm.extract_code("Here:\n```\nv = 0\n```\nDone") == "v = 0"
    assert llm.extract_code("RERAISE") is None
    assert llm.extract_code("```python\nRERAISE\n```") is None
    assert llm.extract_code("<think>hmm</think>```python\nv = 1\n```") == "v = 1"
    assert llm.extract_code("v = 3") == "v = 3"
    assert llm.extract_code("") is None


def test_prompt_is_stable_and_complete(tmp_path):
    a, b = llm.build_prompt(ctx(tmp_path)), llm.build_prompt(ctx(tmp_path))
    assert a == b                                   # no memory addresses, no varying paths
    assert "ZeroDivisionError" in a and "v = 1 / 0" in a and "def f():" in a
    assert "x = [1, 2]" in a and "0x" not in a.replace("0x…", "")
    assert str(tmp_path) not in a


def test_call_then_cache_hit(tmp_path, fake):
    f = fake()
    p = llm.LLMPropose(model="m", api_base=f.url, cache_dir=str(tmp_path / "c"))
    c = ctx(tmp_path)
    assert p(c) == "v = 0"
    assert p(c) == "v = 0"
    assert len(f.requests) == 1
    assert p.stats["calls"] == 1 and p.stats["cache_hits"] == 1
    assert p.stats["prompt_tokens"] == 22 and p.stats["completion_tokens"] == 10
    summary = llm.summarize(str(tmp_path / "c"))
    assert summary["files"] == 1 and summary["models"] == ["m"]


def test_replay_only_never_calls(tmp_path, fake):
    f = fake()
    p = llm.LLMPropose(model="m", api_base=f.url, cache_dir=str(tmp_path / "c"),
                       replay_only=True)
    assert p(ctx(tmp_path)) is None
    assert f.requests == [] and p.stats["misses"] == 1
    llm.LLMPropose(model="m", api_base=f.url, cache_dir=str(tmp_path / "c"))(ctx(tmp_path))
    assert p(ctx(tmp_path)) == "v = 0"              # now in the cache


def test_dry_run_calls_nothing_and_caches_nothing(tmp_path, fake, capsys):
    f = fake()
    p = llm.LLMPropose(model="m", api_base=f.url, cache_dir=str(tmp_path / "c"), dry_run=True)
    assert p(ctx(tmp_path)) is None
    assert f.requests == [] and not (tmp_path / "c").exists()
    assert "prompt (dry run" in capsys.readouterr().err


def test_call_cap(tmp_path, fake):
    f = fake()
    p = llm.LLMPropose(model="m", api_base=f.url, cache_dir=str(tmp_path / "c"), max_calls=1)
    assert p(ctx(tmp_path, "v = 1 / 0")) == "v = 0"
    other = ctx(tmp_path, "v = 2 / 0")              # a different prompt: needs a new call
    assert p(other) is None
    assert p.stats["budget_exhausted"] and len(f.requests) == 1


def test_model_declining_and_unreachable_server(tmp_path, fake):
    f = fake("RERAISE")
    p = llm.LLMPropose(model="m", api_base=f.url, cache_dir=str(tmp_path / "c"))
    assert p(ctx(tmp_path)) is None and p.stats["declined"] == 1
    dead = llm.LLMPropose(model="m", api_base="http://127.0.0.1:9/v1",
                          cache_dir=str(tmp_path / "d"), timeout=2)
    assert dead(ctx(tmp_path)) is None and dead.stats["errors"] == 1


def test_key_is_sent_but_never_stored(tmp_path, fake):
    f = fake()
    secret = "sk-test-SECRET-123"
    p = llm.LLMPropose(model="m", api_base=f.url, cache_dir=str(tmp_path / "c"), api_key=secret)
    p(ctx(tmp_path))
    assert f.auth == [f"Bearer {secret}"]
    for name in os.listdir(tmp_path / "c"):
        assert secret not in (tmp_path / "c" / name).read_text()


def test_end_to_end_with_live_run(tmp_path, fake):
    f = fake("```python\nv = 0\n```")
    prog = tmp_path / "prog.py"
    prog.write_text(textwrap.dedent("""
        import sys
        sys.excepthook = lambda *a: print('original error')
        def f():
            v = 1 / 0
            return v + 5
        print('result', f())
        """))
    env = dict(os.environ, PYTHONPATH=SRC)
    for mode in ("plain", "live"):
        out = subprocess.run([sys.executable, "-m", "transactional_runtime_healing.live_run",
                              str(prog), "--llm", "--mode", mode, "--api-base", f.url,
                              "--model", "m", "--cache-dir", str(tmp_path / "c"),
                              "--log", str(tmp_path / f"{mode}.jsonl")],
                             capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60)
        assert "result 5" in out.stdout, out.stdout + out.stderr
    assert len(f.requests) == 1                      # the second mode was answered from the cache
    # without --llm a rules file is required
    bad = subprocess.run([sys.executable, "-m", "transactional_runtime_healing.live_run",
                          str(prog)], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert bad.returncode != 0 and "rules file is required" in bad.stderr


def test_dotenv_is_read_but_the_environment_wins(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("# c\nTXH_MODEL=from-file\nTXH_API_BASE = 'http://x/v1'\n"
                                   "OTHER=ignored\nTXH_MAX_CALLS=7\n")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.delenv("TXH_IGNORE_DOTENV")
    for k in ("TXH_MODEL", "TXH_API_BASE", "TXH_MAX_CALLS", "OTHER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TXH_MAX_CALLS", "99")
    assert llm.load_dotenv(str(sub)) == str(tmp_path / ".env")   # found in a parent folder
    assert os.environ["TXH_MODEL"] == "from-file"
    assert os.environ["TXH_API_BASE"] == "http://x/v1"
    assert os.environ["TXH_MAX_CALLS"] == "99"                  # real environment wins
    assert "OTHER" not in os.environ                            # only TXH_* is taken
    for k in ("TXH_MODEL", "TXH_API_BASE"):
        monkeypatch.delenv(k, raising=False)


def test_anthropic_style_request_and_answer(tmp_path, monkeypatch):
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["key"] = self.headers.get("x-api-key")
            seen["version"] = self.headers.get("anthropic-version")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            out = json.dumps(dict(content=[dict(type="text", text="```python\nv = 0\n```")],
                                  usage=dict(input_tokens=9, output_tokens=4))).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("TXH_API_STYLE", "anthropic")
    try:
        p = llm.LLMPropose(model="m", api_base=f"http://127.0.0.1:{srv.server_port}/v1",
                           api_key="k-test", cache_dir=str(tmp_path / "c"))
        assert p(ctx(tmp_path)) == "v = 0"
    finally:
        srv.shutdown()
    assert seen["path"] == "/v1/messages" and seen["key"] == "k-test"
    assert seen["version"] and seen["body"]["system"] and seen["body"]["messages"][0]["role"] == "user"
    assert p.stats["prompt_tokens"] == 9 and p.stats["completion_tokens"] == 4


# ---- review fixes -----------------------------------------------------------------------
def test_prose_and_cut_off_replies_are_not_run_as_code():
    c = llm.classify_reply
    assert c("I cannot repair this safely.") == (None, "invalid")
    assert c("RERAISE.") == (None, "decline")
    assert c("```python\nRERAISE\n```") == (None, "decline")
    assert c("```python\nx = 1\n") == (None, "truncated")
    assert c("ok") == (None, "invalid")
    assert c("") == (None, "invalid")
    assert c("x = 1") == ("x = 1", "ok")
    assert c("```python\nx = 1\n```") == ("x = 1", "ok")


def test_errors_are_not_declines_and_are_logged(tmp_path, fake):
    ev = tmp_path / "events.jsonl"
    dead = llm.LLMPropose(model="m", api_base="http://127.0.0.1:9/v1", cache_dir=str(tmp_path / "d"),
                          timeout=2, events_path=str(ev), max_consecutive_errors=2)
    for i in range(3):
        assert dead(ctx(tmp_path, f"v = {i} / 0")) is None
    lines = [json.loads(x) for x in ev.read_text().splitlines()]
    assert [x["outcome"] for x in lines] == ["error", "error", "unavailable"]
    assert dead.stats["errors"] == 2 and dead.stats["declined"] == 0 and dead.stats["unavailable"]
    f = fake("RERAISE", "I am sorry")
    p = llm.LLMPropose(model="m", api_base=f.url, cache_dir=str(tmp_path / "c"),
                       events_path=str(tmp_path / "e2.jsonl"))
    p(ctx(tmp_path, "v = 1 / 0")); p(ctx(tmp_path, "v = 2 / 0"))
    assert p.stats["declined"] == 1 and p.stats["invalid"] == 1 and p.stats["errors"] == 0


def test_run_id_asks_again_and_changes_the_seed(tmp_path, fake):
    f = fake("```python\nv = 0\n```")
    cache = str(tmp_path / "c")
    a = llm.LLMPropose(model="m", api_base=f.url, cache_dir=cache, temperature=0.7, run_id="1")
    b = llm.LLMPropose(model="m", api_base=f.url, cache_dir=cache, temperature=0.7, run_id="2")
    a2 = llm.LLMPropose(model="m", api_base=f.url, cache_dir=cache, temperature=0.7, run_id="1")
    for p in (a, b, a2):
        p(ctx(tmp_path))
    assert len(f.requests) == 2                      # run 2 asked again; run 1 replayed
    assert f.requests[0]["seed"] != f.requests[1]["seed"]


def test_call_cap_is_shared_through_a_budget_file(tmp_path, fake):
    f = fake()
    budget = str(tmp_path / "b")
    for i in range(3):                                 # three "processes" (instances)
        p = llm.LLMPropose(model="m", api_base=f.url, cache_dir=str(tmp_path / "c"),
                           max_calls=2, budget_file=budget)
        p(ctx(tmp_path, f"v = {i} / 0"))
    assert len(f.requests) == 2 and open(budget).read() == "2"


def test_prompt_variants_differ_and_are_part_of_the_cache_key(tmp_path, fake):
    f = fake()
    for name in ("rules", "minimal"):
        llm.LLMPropose(model="m", api_base=f.url, cache_dir=str(tmp_path / "c"),
                       prompt=name)(ctx(tmp_path))
    assert len(f.requests) == 2
    assert "Do not read or write files" in llm.PROMPTS["rules"]
    assert "Do not read or write files" not in llm.PROMPTS["minimal"]
    assert "left side of the failing statement" in llm.system_prompt("minimal", "r = a / b")


def test_return_name_is_only_mentioned_for_a_return():
    assert "_eh_rtn" not in llm.system_prompt("rules", "r = a / b")
    assert "_eh_rtn" in llm.system_prompt("rules", "return a / b")
    assert "_eh_rtn" in llm.system_prompt("minimal", "    return x")


def test_request_options_for_reasoning_models(tmp_path, fake, monkeypatch):
    f = fake()
    monkeypatch.setenv("TXH_TOKEN_PARAM", "max_completion_tokens")
    monkeypatch.setenv("TXH_MAX_TOKENS", "2000")
    monkeypatch.setenv("TXH_SEND_TEMPERATURE", "0")
    monkeypatch.setenv("TXH_EXTRA_BODY", '{"reasoning_effort": "low"}')
    p = llm.LLMPropose(model="m", api_base=f.url, cache_dir=str(tmp_path / "c"))
    assert p(ctx(tmp_path)) == "v = 0"
    body = f.requests[0]
    assert body["max_completion_tokens"] == 2000 and "max_tokens" not in body
    assert "temperature" not in body and body["reasoning_effort"] == "low"
    # the default request is unchanged, and has its own cache entry
    for k in ("TXH_TOKEN_PARAM", "TXH_MAX_TOKENS", "TXH_SEND_TEMPERATURE", "TXH_EXTRA_BODY"):
        monkeypatch.delenv(k)
    q = llm.LLMPropose(model="m", api_base=f.url, cache_dir=str(tmp_path / "c"))
    q(ctx(tmp_path))
    assert f.requests[1]["max_tokens"] == 400 and f.requests[1]["temperature"] == 0.0
    monkeypatch.setenv("TXH_EXTRA_BODY", "[1]")
    with pytest.raises(ValueError):
        llm.LLMPropose(model="m", api_base=f.url)


def test_http_error_shows_the_apis_message_but_not_for_key_errors(tmp_path):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            code = 401 if "bad-key" in (self.headers.get("Authorization") or "") else 400
            msg = "Incorrect API key provided: sk-...abcd" if code == 401 else \
                "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."
            out = json.dumps(dict(error=dict(message=msg))).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/v1"
    try:
        p = llm.LLMPropose(model="m", api_base=url, cache_dir=str(tmp_path / "c"))
        with pytest.raises(llm.LLMError, match="max_completion_tokens"):
            p._chat([dict(role="user", content="x")])
        p2 = llm.LLMPropose(model="m", api_base=url, api_key="bad-key", cache_dir=str(tmp_path / "d"))
        with pytest.raises(llm.LLMError) as e:
            p2._chat([dict(role="user", content="x")])
        assert "HTTP 401" in str(e.value) and "sk-" not in str(e.value)
    finally:
        srv.shutdown()


def test_transient_errors_are_retried_and_permanent_ones_are_not(tmp_path):
    from transactional_runtime_healing.llm import LLMError, LLMPropose
    p = LLMPropose(model="m", api_base="http://127.0.0.1:9/v1", cache_dir=str(tmp_path))
    calls = []

    def flaky(messages):
        calls.append(1)
        if len(calls) < 3:
            e = LLMError("HTTP 503")
            e.transient = True
            raise e
        return "ok", {}
    p._chat_once = flaky
    assert p._chat([]) == ("ok", {}) and len(calls) == 3

    calls.clear()

    def refused(messages):
        calls.append(1)
        raise LLMError("HTTP 400")
    p._chat_once = refused
    with pytest.raises(LLMError):
        p._chat([])
    assert len(calls) == 1