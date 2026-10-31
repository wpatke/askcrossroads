"""A stand-in for the Anthropic Messages API on localhost, for offline tests.

The anthropic SDK sends its requests to ANTHROPIC_BASE_URL when that is set. Pointing it here
lets a test run the REAL AnthropicClient, and even the real askcrossroads command, with
scripted replies and no network. Nothing in AskCrossroads is replaced: only the model's words
are scripted.

    with ProviderStub() as provider:
        provider.reply_sql("SELECT 1 AS one")
        provider.reply_chart({"mark": "bar", "encoding": {...}})
        # ... point ANTHROPIC_BASE_URL at provider.url, then ask a question ...
        provider.requests   # what the client sent, in order

reply_error(..., echo_key=True) puts the API key the client sent into the error body, the way
a careless provider might. That is how the tests prove the key cannot leak even then.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _message(text):
    """A minimal successful Messages API reply carrying one text block."""
    return {
        "id": "msg_stub",
        "type": "message",
        "role": "assistant",
        "model": "stub",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _error(status, message):
    kind = "authentication_error" if status == 401 else "invalid_request_error"
    return {"type": "error", "error": {"type": kind, "message": message}}


class ProviderStub:
    """Serves queued replies, one per request, in order. Records every request."""

    def __init__(self):
        self.requests = []   # {"path", "api_key", "body"} per request, oldest first
        self._replies = []
        self._condition = threading.Condition()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                status, payload, delay = stub._take(self.path, self.headers.get("x-api-key"), body)
                if delay:
                    time.sleep(delay)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass  # keep test output quiet

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exception):
        self._server.shutdown()
        self._server.server_close()
        return False

    # --- Scripting replies ----------------------------------------------------------

    def reply_text(self, text, delay=0):
        """A successful reply whose text is `text`, after `delay` seconds."""
        self._queue({"status": 200, "payload": _message(text), "delay": delay})

    def reply_sql(self, sql, delay=0):
        """The SQL call's structured reply: answerable, with this SQL (spec §6)."""
        self.reply_text(json.dumps({"answerable": True, "sql": sql, "explanation": None}), delay)

    def reply_chart(self, spec):
        self.reply_text(json.dumps(spec))

    def reply_error(self, status, echo_key=False):
        """An HTTP error. 400 and 401 are used because the SDK does not retry them."""
        self._queue({"status": status, "payload": None, "delay": 0, "echo_key": echo_key})

    def wait_for_requests(self, count, timeout=10):
        """True once at least `count` requests have arrived."""
        with self._condition:
            return self._condition.wait_for(lambda: len(self.requests) >= count, timeout)

    def clear(self):
        """Forget every queued reply and recorded request, so the next test starts clean.

        The browser tests share one stub across a module, and call this between tests.
        """
        with self._condition:
            self._replies.clear()
            self.requests.clear()

    # --- Internals -----------------------------------------------------------------

    def _queue(self, reply):
        with self._condition:
            self._replies.append(reply)

    def _take(self, path, api_key, body):
        with self._condition:
            self.requests.append({"path": path, "api_key": api_key, "body": body})
            reply = self._replies.pop(0) if self._replies else None
            self._condition.notify_all()
        if reply is None:
            # 400 rather than 500, because the SDK retries a 500 and the test would slow down.
            return 400, _error(400, "The provider stub had no reply queued."), 0
        payload = reply["payload"]
        if payload is None:
            message = "Request rejected."
            if reply["echo_key"]:
                message = f"Request rejected for API key {api_key}."
            payload = _error(reply["status"], message)
        return reply["status"], payload, reply["delay"]
