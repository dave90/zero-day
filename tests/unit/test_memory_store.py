"""Per-agent memory store: replace-model, missing-file, path sanitization, isolation."""
import pytest

from zero.tools._memroot import memory_root, path_for
from zero.tools.memory import read_memory, write_memory


@pytest.fixture
def rooted(tmp_path, monkeypatch):
    monkeypatch.setenv("ZERO_MEMORY_ROOT", str(tmp_path))
    return tmp_path


def test_root_from_env(rooted):
    assert memory_root() == rooted.resolve()


def test_write_then_read_roundtrip(rooted):
    write_memory("gino_2", "tried SQLi on sort param; reflected error")
    assert read_memory("gino_2") == "tried SQLi on sort param; reflected error"


def test_write_replaces_not_appends(rooted):
    write_memory("gino_2", "first" * 100)
    write_memory("gino_2", "second")
    body = read_memory("gino_2")
    assert body == "second"  # replaced, not appended
    assert "first" not in body


def test_read_missing_is_empty_not_error(rooted):
    # A fresh agent has no memory yet — that must read as empty, not blow up.
    assert read_memory("never_written") == ""


def test_isolation_between_sessions(rooted):
    write_memory("gino_2", "gino's notes")
    write_memory("pina_3", "pina's notes")
    assert read_memory("gino_2") == "gino's notes"
    assert read_memory("pina_3") == "pina's notes"


# --- path sanitization: an id can never escape the memory root ---


@pytest.mark.parametrize(
    "session_id",
    ["../../etc/passwd", "/etc/passwd", "validator[dino_5 /api/x]", "a/b/c", ".."],
)
def test_path_for_stays_inside_root(rooted, session_id):
    p = path_for(session_id)
    assert p.parent == rooted.resolve()
    assert p.suffix == ".md"


def test_label_shaped_id_is_flattened(rooted):
    # The validator label shape, if it ever reaches the store, collapses to a flat
    # contained filename rather than nesting/escaping.
    write_memory("validator[dino_5 /api/x]", "verdict pending")
    files = list(rooted.glob("*.md"))
    assert len(files) == 1
    assert files[0].parent == rooted.resolve()
