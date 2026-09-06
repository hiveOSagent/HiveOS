"""SPRINT_7 Batch B — pre-flight smoke tests for learned skills.

Adds 12 tests covering:
  - SkillTemplate smoke fields exist (default values + persistence)
  - run_smoke_test verdicts (pass / fail-dangerous / fail-dag / error-syntax / fail-falsy)
  - propose_skill integration (smoke runs before status flip; force overrides)
  - gateway surfaces smoke_result + smoke_log
  - isolation + truncation safety properties
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from hive.tools.learned_skills import (
    ALL_SMOKE_RESULTS,
    SMOKE_ERROR,
    SMOKE_FAIL,
    SMOKE_NONE,
    SMOKE_PASS,
    STATUS_PROPOSED,
    STATUS_SMOKE_FAILED,
    LearnedSkillStore,
    SkillTemplate,
    propose_skill,
    run_smoke_test,
)


# ---- helpers ----------------------------------------------------------------

class _FakeRegistry:
    """Minimal duck-typed registry used by most smoke tests."""

    def __init__(self, *names: str) -> None:
        self._tools = {n: object() for n in names}

    def snapshot(self) -> dict:
        return dict(self._tools)


def _mk_template(code: str, *, pattern=("foo", "bar"), name="learned_test_x1") -> SkillTemplate:
    """Build a SkillTemplate with the given body, skipping smoke for clean setup."""
    return SkillTemplate(
        id=name, name=name, description="t",
        pattern=tuple(pattern), params={"type": "object"}, code=code,
        status=STATUS_PROPOSED, created_ts=0.0,
    )


# ---- 1: SkillTemplate smoke fields -----------------------------------------

def test_skill_template_has_smoke_fields():
    """The SkillTemplate dataclass has smoke_result + smoke_log with sensible defaults."""
    t = SkillTemplate(
        id="x", name="x", description="x", pattern=(), params={}, code="",
    )
    assert hasattr(t, "smoke_result")
    assert hasattr(t, "smoke_log")
    assert t.smoke_result == SMOKE_NONE
    assert t.smoke_log == ""
    # to_dict / from_dict round-trip both fields.
    d = t.to_dict()
    assert d["smoke_result"] == SMOKE_NONE
    assert d["smoke_log"] == ""
    t2 = SkillTemplate.from_dict(d)
    assert t2.smoke_result == SMOKE_NONE
    assert t2.smoke_log == ""
    # ALL_SMOKE_RESULTS exposes the four values for callers.
    assert set(ALL_SMOKE_RESULTS) == {SMOKE_NONE, SMOKE_PASS, SMOKE_FAIL, SMOKE_ERROR}


# ---- 2: pass for valid body -------------------------------------------------

def test_run_smoke_test_passes_for_valid_body():
    """A body that awaits call_tool("foo", {}) returns a ToolResult -> smoke pass."""
    code = (
        "async def run(call_tool, args):\n"
        "    r = await call_tool('foo', {})\n"
        "    return [r]\n"
    )
    t = _mk_template(code)
    reg = _FakeRegistry("foo", "bar")
    out = run_smoke_test(t, reg)
    assert out.smoke_result == SMOKE_PASS
    assert out.smoke_log == ""


# ---- 3: fail on dangerous pattern ------------------------------------------

def test_run_smoke_test_ignores_dangerous_legacy_code():
    """Legacy code is data: the declarative pattern is the only executable plan."""
    code = (
        "def run(call_tool, args):\n"
        "    return eval('1+1')\n"
    )
    t = _mk_template(code)
    reg = _FakeRegistry("foo", "bar")
    out = run_smoke_test(t, reg)
    assert out.smoke_result == SMOKE_PASS


def test_run_smoke_test_ignores_shell_text_in_legacy_code():
    code = "async def run(call_tool, args):\n    return ['rm -rf /']\n"
    t = _mk_template(code)
    out = run_smoke_test(t, _FakeRegistry("foo", "bar"))
    assert out.smoke_result == SMOKE_PASS


# ---- 4: fail on unknown tool (DAG safety) -----------------------------------

def test_run_smoke_test_fails_on_unknown_tool():
    """Unknown patterns fail without evaluating retained legacy code."""
    code = (
        "async def run(call_tool, args):\n"
        "    raise RuntimeError('legacy-code-must-not-run')\n"
    )
    t = _mk_template(code, pattern=("ghost",))
    out = run_smoke_test(t, _FakeRegistry("foo", "bar"))
    assert out.smoke_result == SMOKE_FAIL
    assert "ghost" in out.smoke_log
    assert "legacy-code-must-not-run" not in out.smoke_log

# ---- 5: error on compile fail -----------------------------------------------

def test_run_smoke_test_ignores_invalid_legacy_code():
    code = "def x: pass\n"   # SyntaxError: missing parentheses
    t = _mk_template(code)
    out = run_smoke_test(t, _FakeRegistry("foo", "bar"))
    assert out.smoke_result == SMOKE_PASS


def test_run_smoke_test_never_executes_legacy_code():
    code = (
        "async def run(call_tool, args):\n"
        "    raise RuntimeError('boom')\n"
    )
    t = _mk_template(code)
    out = run_smoke_test(t, _FakeRegistry("foo", "bar"))
    assert out.smoke_result == SMOKE_PASS


# ---- 6: fail on falsy return -----------------------------------------------

def test_run_smoke_test_ignores_legacy_return_value():
    code = (
        "async def run(call_tool, args):\n"
        "    return None\n"
    )
    t = _mk_template(code)
    out = run_smoke_test(t, _FakeRegistry("foo", "bar"))
    assert out.smoke_result == SMOKE_PASS


# ---- 7: propose_skill calls smoke before flipping status -------------------

def test_propose_skill_runs_smoke_before_proposed(monkeypatch):
    """propose_skill runs run_smoke_test before deciding the status."""
    seen = {}
    original = run_smoke_test

    def spy(template, registry):
        seen["called"] = True
        seen["status_before"] = template.status
        return original(template, registry)

    monkeypatch.setattr("hive.tools.learned_skills.run_smoke_test", spy)
    reg = _FakeRegistry("foo", "bar")
    t = propose_skill(("foo", "bar"), registry=reg)
    assert seen["called"] is True
    # Smoke ran while template was still in proposed state.
    assert seen["status_before"] == STATUS_PROPOSED
    # Smoke passed (registry has all referenced tools) -> status stays proposed.
    assert t.status == STATUS_PROPOSED
    assert t.smoke_result == SMOKE_PASS


# ---- 8: smoke_failed status on dangerous pattern ---------------------------

def test_propose_skill_status_smoke_failed_on_unknown_pattern():
    """A pattern absent from the registry cannot be proposed automatically."""
    out = propose_skill(("missing_tool",), registry=_FakeRegistry("foo"))
    assert out.status == STATUS_SMOKE_FAILED
    assert out.smoke_result == SMOKE_FAIL
    assert "missing_tool" in out.smoke_log

def test_propose_skill_force_proposes_anyway():
    """force=True preserves the recorded declarative validation failure."""
    out = propose_skill(("missing_tool",), registry=_FakeRegistry("foo"), force=True)
    assert out.status == STATUS_PROPOSED
    assert out.smoke_result == SMOKE_FAIL
    assert "missing_tool" in out.smoke_log

# ---- 10: GET /skills/learned/{id} surfaces smoke fields --------------------

def _client(monkeypatch, tmp_path):
    from hive.runtime import HiveOS
    monkeypatch.setenv("HIVE_SECRET", "test-secret")
    monkeypatch.setenv("HIVE_HOST", "127.0.0.1")
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HIVE_STATE_DB", str(tmp_path / "state.sqlite"))
    import hive.core.config as cfg_mod
    cfg_mod._CONFIG = None
    hive = HiveOS.build()
    from hive.gateway.app import create_app
    app = create_app(hive)
    return TestClient(app), hive


def test_get_learned_skill_returns_smoke_fields(monkeypatch, tmp_path):
    """GET /skills/learned/{id} exposes smoke_result + smoke_log on the JSON."""
    client, hive = _client(monkeypatch, tmp_path)
    r = client.post(
        "/skills/learned/propose",
        headers={"X-Hive-Token": "test-secret"},
        json={"pattern": ["hive_status", "read_file", "shell"], "description": "demo"},
    )
    assert r.status_code == 200, r.text
    template_id = r.json()["id"]
    detail = client.get(
        f"/skills/learned/{template_id}",
        headers={"X-Hive-Token": "test-secret"},
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["smoke_result"] == SMOKE_PASS
    assert body["smoke_log"] == ""

    # Also confirm a smoke_failed template surfaces both fields with content.
    r2 = client.post(
        "/skills/learned/propose",
        headers={"X-Hive-Token": "test-secret"},
        json={"pattern": ["definitely_not_in_registry"]},
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["status"] == STATUS_SMOKE_FAILED
    assert body2["smoke_result"] == SMOKE_FAIL
    assert body2["smoke_log"]  # populated reason


# ---- 11: smoke_log truncation ----------------------------------------------

def test_smoke_log_truncated_to_500_chars():
    """A large invalid declarative pattern cannot blow up persisted diagnostics."""
    t = _mk_template("", pattern=tuple(f"missing_{i}" for i in range(200)))
    out = run_smoke_test(t, _FakeRegistry())
    assert out.smoke_result == SMOKE_FAIL
    assert len(out.smoke_log) <= 500

# ---- 12: isolated namespace -------------------------------------------------

def test_run_smoke_test_ignores_legacy_code_as_data():
    """A persisted legacy body cannot affect a declarative smoke verdict."""
    t = _mk_template("raise RuntimeError('must never execute')", pattern=("foo",))
    out = run_smoke_test(t, _FakeRegistry("foo"))
    assert out.smoke_result == SMOKE_PASS
    assert out.smoke_log == ""

# ---- extra: persisted smoke fields survive a round-trip --------------------

def test_persisted_smoke_fields_round_trip(tmp_path):
    """SQLite save -> get preserves smoke_result and smoke_log."""
    db = tmp_path / "ls.sqlite"
    store = LearnedSkillStore(db)
    t = propose_skill(("alpha", "beta"))
    t.smoke_result = SMOKE_FAIL
    t.smoke_log = "test-reason"
    store.save(t)
    fetched = store.get(t.id)
    assert fetched is not None
    assert fetched.smoke_result == SMOKE_FAIL
    assert fetched.smoke_log == "test-reason"
    store.close()



# ---- 14: SQLite migration (Block 1) -----------------------------------------

def test_store_migrates_old_schema(tmp_path):
    """A pre-Batch-B DB (no smoke_result / smoke_log columns) is migrated on
    open; ``save()`` then works without raising ``OperationalError``.

    Pre-Batch-B DBs read fine via defensive try/except in ``_row_to_template``
    but ``save()`` raises because the INSERT statement references columns
    that don't exist. The migration adds the missing columns so save succeeds.
    """
    import sqlite3 as _sql

    db = tmp_path / "pre_batch_b.sqlite"
    # Build a realistic pre-Batch-B schema — has every column the new INSERT
    # statement references EXCEPT smoke_result and smoke_log.
    conn = _sql.connect(str(db))
    conn.executescript("""
        CREATE TABLE learned_skills(
          id          TEXT PRIMARY KEY,
          name        TEXT NOT NULL,
          description TEXT NOT NULL,
          pattern     TEXT NOT NULL,
          params      TEXT NOT NULL,
          code        TEXT NOT NULL,
          status      TEXT NOT NULL DEFAULT 'proposed',
          created_ts  REAL NOT NULL,
          approved_ts REAL,
          use_count   INTEGER NOT NULL DEFAULT 0,
          last_used_ts REAL,
          category    TEXT NOT NULL DEFAULT 'learned',
          dangerous   INTEGER NOT NULL DEFAULT 0,
          notes       TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE tool_sequences(
          seq_key   TEXT PRIMARY KEY,
          pattern   TEXT NOT NULL,
          count     INTEGER NOT NULL,
          last_seen REAL NOT NULL
        );
    """)
    # Plant a row in the old schema so the migration has to backfill it.
    conn.execute(
        "INSERT INTO learned_skills(id, name, description, pattern, params,"
        " code, status, created_ts) VALUES (?,?,?,?,?,?,?,?)",
        ("old_id", "old_skill", "old desc", "[]", "{}", "pass", "proposed", 1.0),
    )
    conn.commit()
    conn.close()

    # Open the store — migration runs in __init__.
    store = LearnedSkillStore(db)
    try:
        # Confirm the columns exist after migration.
        cols = {r["name"] for r in store._db.execute(
            "PRAGMA table_info(learned_skills)"
        ).fetchall()}
        assert "smoke_result" in cols
        assert "smoke_log" in cols

        # Old row reads back with default smoke fields (SMOKE_NONE / "").
        old = store.get("old_id")
        assert old is not None
        assert old.smoke_result == SMOKE_NONE
        assert old.smoke_log == ""

        # save() must not raise OperationalError now that the columns exist.
        t = _mk_template(
            "async def run(call_tool, args):\n    return ['ok']\n",
            name="learned_new_xx",
        )
        t.smoke_result = SMOKE_PASS
        t.smoke_log = "ok"
        store.save(t)
        fetched = store.get(t.id)
        assert fetched is not None
        assert fetched.smoke_result == SMOKE_PASS
        assert fetched.smoke_log == "ok"
    finally:
        store.close()


def test_store_migration_is_idempotent(tmp_path):
    """Running the migration twice does not raise and is a no-op the second time.

    Re-opening the store (or invoking ``_migrate`` directly) on an
    already-migrated DB must be safe — no duplicate-column errors, no schema
    drift.
    """
    db = tmp_path / "idem.sqlite"

    # First open — fresh DB, no migration needed but path is exercised.
    store = LearnedSkillStore(db)
    try:
        store._migrate()
        store._migrate()
        # If the second call had added duplicate columns, the third open
        # would explode at ALTER; instead we just re-open and confirm it's
        # still a healthy store.
    finally:
        store.close()

    store2 = LearnedSkillStore(db)
    try:
        store2._migrate()
        # Schema check: only one smoke_result column.
        smoke_cols = [r for r in store2._db.execute(
            "PRAGMA table_info(learned_skills)"
        ).fetchall() if r["name"] == "smoke_result"]
        assert len(smoke_cols) == 1
        # save() still works on the re-opened + re-migrated DB.
        t = _mk_template(
            "async def run(call_tool, args):\n    return ['ok']\n",
            name="learned_idem_xx",
        )
        store2.save(t)
        assert store2.get(t.id) is not None
    finally:
        store2.close()
