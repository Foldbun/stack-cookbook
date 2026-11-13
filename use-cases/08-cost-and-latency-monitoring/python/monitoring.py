"""Prometheus metrics and a spend guard around stack_client.py. One file: copy it next to stack_client.py.

    from monitoring import Monitor, MonitoredClient, SpendGuard
    guard = SpendGuard(max_per_hour=50, mode="alert", on_alert=lambda info: print("over budget", info))
    monitor = Monitor(guard=guard)
    client = MonitoredClient(StackClient(), monitor)     # same methods as StackClient: post, batch, upload, models, ...
    monitor.serve(9108)                                   # GET http://localhost:9108/metrics

Everything is read from what each answer already carries (`cost`, `billing`, `balance`, `timing`, `cache`, the error
code), so there is nothing to ask the API for except an optional `GET /v1/whoami`, which is free and not metered.
stack_client.py itself is not modified: MonitoredClient wraps a client object and forwards what it does not know.
"""

import collections
import itertools
import threading
import time

import stack_client as sc
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest, start_http_server

# Milliseconds. The API's own timing starts at a few ms and a single call may take up to 90 s.
LATENCY_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 90000)


class SpendGuardTripped(sc.StackError):
    """A call refused locally because the spend of the last hour is over the limit (`mode="reject"`). Never sent."""

    def __init__(self, message):
        super().__init__(0, "spend_guard_tripped", message)


# region guard
class SpendGuard:
    """Sliding-window spend rate. `max_per_hour` credits over the last `window_s` seconds (default one hour).

    mode "alert":  `on_alert(info)` runs once each time the spend crosses the limit; calls are not stopped.
    mode "reject": `check()` raises SpendGuardTripped while the spend of the window is over the limit.
    basis "charged" counts what the account is debited (calls inside a daily allowance count 0); "list" counts the list price.
    Pure logic: pass `clock` (a function returning seconds) to test it without waiting."""

    def __init__(self, max_per_hour, *, mode="alert", basis="charged", on_alert=None, window_s=3600.0, clock=time.monotonic):
        if mode not in ("alert", "reject") or basis not in ("charged", "list"):
            raise ValueError('mode is "alert" or "reject"; basis is "charged" or "list"')
        self.max_per_hour, self.mode, self.basis, self.on_alert = max_per_hour, mode, basis, on_alert
        self.window_s, self.clock = window_s, clock
        self.records, self.alerts, self.tripped = 0, 0, False  # calls recorded, alerts raised, over the limit right now
        self._events, self._micro, self._lock = collections.deque(), 0, threading.Lock()

    def _expire(self, now):
        while self._events and self._events[0][0] <= now - self.window_s:
            self._micro -= self._events.popleft()[1]

    def spent(self):
        """Credits spent inside the window now."""
        with self._lock:
            self._expire(self.clock())
            return self._micro / 1e6

    def check(self):
        """Call before sending: in mode "reject", raises SpendGuardTripped while the window's spend is over the limit."""
        if self.mode != "reject":
            return
        with self._lock:
            self._expire(self.clock())
            over, spent = self._micro > self.max_per_hour * 1e6, self._micro / 1e6
        if over:
            raise SpendGuardTripped(
                f"call refused: {spent:g} credits in the last {self.window_s / 60:g} minutes pass the limit of {self.max_per_hour:g} credits"
            )

    def record(self, amount):
        """Add the spend of one finished call (0 is fine; it still counts as a call). Returns True when this call tripped the guard."""
        with self._lock:
            now = self.clock()
            self._expire(now)
            micro = round(amount * 1e6)
            self._events.append((now, micro))
            self._micro += micro
            self.records += 1
            over = self._micro > self.max_per_hour * 1e6
            fired = over and not self.tripped
            self.tripped = over
            info = {
                "call": self.records,
                "spent": self._micro / 1e6,
                "max_per_hour": self.max_per_hour,
                "window_s": self.window_s,
                "mode": self.mode,
                "basis": self.basis,
            }
            if fired:
                self.alerts += 1
        if fired and self.on_alert:
            self.on_alert(info)
        return fired

    # endregion


class Monitor:
    """The metrics. `registry` defaults to a private one, so importing this file changes nothing global."""

    def __init__(self, registry=None, guard=None, clock=time.perf_counter, wall=time.time):
        self.registry, self.guard, self.clock, self.wall = registry or CollectorRegistry(), guard, clock, wall
        r = self.registry
        self.credits = Counter(
            "stack_credits_total",
            "Credits of every priced line; charged=true was debited, false was covered by a daily allowance.",
            ["model", "unit", "charged"],
            registry=r,
        )
        self.calls = Counter(
            "stack_calls_total",
            "Calls by outcome: ok, replayed, error, expired or refused (never sent); code is the API error code.",
            ["model", "status", "code"],
            registry=r,
        )
        self.cache_hits = Counter(
            "stack_cache_hits_total", "SAM calls whose image encoding was already cached (encode_image fee waived).", ["model"], registry=r
        )
        self.cache_misses = Counter("stack_cache_misses_total", "SAM calls that paid for encoding the image.", ["model"], registry=r)
        self.cache_saved = Counter("stack_cache_saved_credits_total", "List price of the encoding fees waived by cache hits.", ["model"], registry=r)
        self.latency = Histogram(
            "stack_latency_ms",
            "Milliseconds: phase queue and processing from the API's timing, client_total measured in this process.",
            ["model", "phase"],
            buckets=LATENCY_BUCKETS_MS,
            registry=r,
        )
        self.balance = Gauge("stack_balance_credits", "Account balance after the last answer or whoami refresh.", registry=r)
        self.balance_at = Gauge("stack_balance_updated_timestamp_seconds", "When stack_balance_credits was last set.", registry=r)
        self.allow_used = Gauge("stack_daily_allowance_used", "Included daily calls used today (UTC) per pool.", ["pool"], registry=r)
        self.allow_limit = Gauge("stack_daily_allowance_limit", "Included daily calls per pool.", ["pool"], registry=r)
        self.window_spend = Gauge("stack_spend_window_credits", "Spend inside the guard's window (what the guard compares with its limit).", registry=r)
        self.guard_tripped = Gauge("stack_spend_guard_tripped", "1 while the spend of the window is over the guard's limit.", registry=r)
        self._tags = itertools.count(1)

    # region observe
    def observe(self, model, result, *, replayed=False, client_ms=None):
        """One successful call (or `ok` batch item): `result` is the answer body."""
        model = result.get("model") or model
        self.calls.labels(model, "replayed" if replayed else "ok", "none").inc()
        if client_ms is not None:
            self.latency.labels(model, "client_total").observe(client_ms)
        billing = result.get("billing") or {}
        cost = result.get("cost", 0)
        if not replayed:  # a replay returns the stored answer of a call that was already counted
            timing = result.get("timing") or {}
            for phase, key in (("queue", "queueMs"), ("processing", "processingMs")):
                if key in timing:
                    self.latency.labels(model, phase).observe(timing[key])
            charged = "true" if billing.get("charged", cost > 0) else "false"
            for line in billing.get("lines") or [{"unit": "unknown", "amount": cost, "waived": False}]:
                if line.get("waived"):
                    self.cache_saved.labels(model).inc(round(line["unitPrice"] * line["quantity"], 6))
                elif line["amount"]:
                    self.credits.labels(model, line["unit"], charged).inc(line["amount"])
            if result.get("cache") == "hit":
                self.cache_hits.labels(model).inc()
            elif result.get("cache") == "miss":
                self.cache_misses.labels(model).inc()
            if "balance" in result:
                self._set_balance(result["balance"])
            if billing.get("pool") and billing.get("allowance"):
                self.allow_used.labels(billing["pool"]).set(billing["allowance"]["used"])
                self.allow_limit.labels(billing["pool"]).set(billing["allowance"]["limit"])
        by_list = self.guard is not None and self.guard.basis == "list"
        self._guard_record(0 if replayed else billing.get("total", cost) if by_list else cost)

    def failure(self, model, error, *, status="error", client_ms=None):
        """One failed call: `error` is a StackError (or anything with `.code`). Local refusals count as "refused"."""
        code = getattr(error, "code", None) or type(error).__name__
        if getattr(error, "status", None) == 0 and code in ("budget_exceeded", "spend_guard_tripped"):
            status = "refused"
        self.calls.labels(model, status, code).inc()
        if client_ms is not None:
            self.latency.labels(model, "client_total").observe(client_ms)
        self._guard_record(0)

    def _set_balance(self, value):
        self.balance.set(value)
        self.balance_at.set(self.wall())

    def _guard_record(self, amount):
        if self.guard:
            self.guard.record(amount)
            self.window_spend.set(self.guard.spent())
            self.guard_tripped.set(1 if self.guard.tripped else 0)

    # endregion

    # region account
    def refresh_account(self, client):
        """GET /v1/whoami (free, not metered, no rate-limit token): balance and today's daily allowances."""
        info = client.get("/v1/whoami")
        self._set_balance(info["balance"])
        for a in info.get("dailyAllowances", []):
            self.allow_used.labels(a["pool"]).set(a["used"])
            self.allow_limit.labels(a["pool"]).set(a["limit"])
        return info

    def refresh_every(self, client, seconds):
        """Refresh in a background thread; `.set()` the returned event to stop it. Errors are swallowed: the gauges just go stale."""
        stop = threading.Event()

        def loop():
            while not stop.wait(seconds):
                try:
                    self.refresh_account(client)
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=loop, daemon=True, name="stack-whoami").start()
        return stop

    # endregion

    def next_tag(self):
        return f"mon-{next(self._tags)}"

    def serve(self, port, addr="0.0.0.0"):  # noqa: S104
        """Expose GET /metrics on `port` in a daemon thread. Returns (server, thread); `server.shutdown()` stops it."""
        return start_http_server(port, addr=addr, registry=self.registry)

    def exposition(self):
        return generate_latest(self.registry).decode()

    def samples(self, name):
        """[(labels, value)] of every series of one sample name, e.g. samples("stack_credits_total")."""
        return [(dict(s.labels), s.value) for family in self.registry.collect() for s in family.samples if s.name == name]

    def snapshot(self, skip=lambda name, labels: False):
        """The stack_* samples as {'name{label="v",...}': value}, without histogram buckets and `_created` stamps."""
        out = {}
        for family in self.registry.collect():
            for s in family.samples:
                if s.name.endswith(("_bucket", "_created")) or skip(s.name, s.labels):
                    continue
                labels = ",".join(f'{k}="{v}"' for k, v in s.labels.items())
                out[f"{s.name}{{{labels}}}" if labels else s.name] = round(s.value, 6)
        return out


# region wrapper
class MonitoredClient:
    """Wraps a StackClient. `post` and `batch` are measured; every other attribute (`upload`, `models`, `get`, `calls`,
    `summary`, `ledger`, ...) is the wrapped client's."""

    def __init__(self, client, monitor):
        self._client, self.monitor = client, monitor

    def __getattr__(self, name):
        return getattr(self._client, name)

    @staticmethod
    def _model(path, body):
        return body.get("model") or sc.DEFAULT_MODEL.get(path) or "unknown"

    def _replayed(self, tag):
        """Whether the client's ledger says the answer of this call was a replay of a stored one."""
        ledger = getattr(self._client, "ledger", None) or []
        for t, row in reversed(ledger[-32:]):
            if t == tag:
                return bool(row.get("replayed"))
        return False

    def post(self, path, body, idempotency_key=None, *, tag=None, **kwargs):
        model = self._model(path, body)
        tag = tag or self.monitor.next_tag()
        try:
            if self.monitor.guard:
                self.monitor.guard.check()
        except SpendGuardTripped as err:
            self.monitor.failure(model, err)
            raise
        t0 = self.monitor.clock()
        try:
            result = self._client.post(path, body, idempotency_key, tag=tag, **kwargs)
        except Exception as err:  # a StackError, or anything unexpected: counted, then raised unchanged
            self.monitor.failure(model, err, client_ms=(self.monitor.clock() - t0) * 1000)
            raise
        self.monitor.observe(model, result, replayed=self._replayed(tag), client_ms=(self.monitor.clock() - t0) * 1000)
        return result

    def batch(self, endpoint, body, idempotency_key=None, *, tag=None):
        """Like StackClient.batch (yields (event, data)); every `item` event is counted with its own cost and timing."""
        model = self._model(f"/v1/{endpoint}", body)
        if self.monitor.guard:
            self.monitor.guard.check()
        try:
            for event, data in self._client.batch(endpoint, body, idempotency_key, tag=tag):
                if event == "item":
                    status = data.get("status")
                    if status == "ok":
                        self.monitor.observe(model, data["result"], replayed=bool(data.get("replayed")))
                    else:
                        error = data.get("error") or {}
                        self.monitor.failure(
                            model, sc.StackError(0, error.get("code") or status, error.get("message", "")), status="expired" if status == "expired" else "error"
                        )
                yield event, data
        except Exception as err:  # the stream itself failed (cut, timed out, refused)
            self.monitor.failure(model, err)
            raise

    def whoami(self):
        return self.monitor.refresh_account(self._client)

    # endregion
