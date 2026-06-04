"""On-disk findings store: confirmed/ rejected/ pending/ as markdown+frontmatter.

The index (`.pentest/findings/index.json`) is a convenience for fast dedup
lookups — it can be rebuilt from disk at any time (the disk is the source
of truth, per plan §3.3.1).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import yaml  # type: ignore[import-not-found]

from zero.findings.schema import FindingRecord, Verdict


_FRONTMATTER_DELIM = "---\n"


class FindingsStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.dirs: dict[Verdict, Path] = {
            "confirmed": root / "confirmed",
            "rejected": root / "rejected",
            "pending": root / "pending",
        }
        for d in self.dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        self.index_path = root / "index.json"

    # ------------------------------------------------------------- index

    def index(self) -> dict[str, dict]:
        """{dedup_hash → {id, verdict, title}}. Rebuilt from disk if missing."""
        if not self.index_path.exists():
            return self.rebuild_index()
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self.rebuild_index()

    def rebuild_index(self) -> dict[str, dict]:
        idx: dict[str, dict] = {}
        for verdict, d in self.dirs.items():
            for f in d.glob("*.md"):
                rec = self._read(f)
                if rec is None:
                    continue
                idx[rec.dedup_hash] = {
                    "id": rec.id,
                    "verdict": verdict,
                    "title": rec.finding.title,
                    "path": str(f.relative_to(self.root)),
                }
        self._write_index(idx)
        return idx

    def lookup_by_hash(self, h: str) -> dict | None:
        return self.index().get(h)

    def _write_index(self, idx: dict) -> None:
        self.index_path.write_text(json.dumps(idx, indent=2), encoding="utf-8")

    # ----------------------------------------------------------- write/read

    def submit_pending(self, rec: FindingRecord) -> Path:
        return self._save(rec, "pending")

    def promote(self, rec: FindingRecord, verdict: Verdict) -> Path:
        """Move a record between buckets and update its verdict in-place."""
        # Remove any old copy.
        self._remove_by_id(rec.id)
        return self._save(rec, verdict)

    def all_records(self, verdict: Verdict | None = None) -> list[FindingRecord]:
        out: list[FindingRecord] = []
        items = self.dirs.items() if verdict is None else [(verdict, self.dirs[verdict])]
        for _v, d in items:
            for f in sorted(d.glob("*.md")):
                rec = self._read(f)
                if rec is not None:
                    out.append(rec)
        return out

    def _save(self, rec: FindingRecord, verdict: Verdict) -> Path:
        path = self.dirs[verdict] / f"{rec.id}.md"
        path.write_text(self._serialize(rec), encoding="utf-8")
        # Update index.
        idx = self.index()
        idx[rec.dedup_hash] = {
            "id": rec.id,
            "verdict": verdict,
            "title": rec.finding.title,
            "path": str(path.relative_to(self.root)),
        }
        self._write_index(idx)
        return path

    def _remove_by_id(self, fid: str) -> None:
        for d in self.dirs.values():
            p = d / f"{fid}.md"
            if p.exists():
                p.unlink()

    def _read(self, p: Path) -> FindingRecord | None:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            return None
        if not text.startswith(_FRONTMATTER_DELIM):
            return None
        rest = text[len(_FRONTMATTER_DELIM):]
        try:
            end = rest.index("\n" + _FRONTMATTER_DELIM)
        except ValueError:
            return None
        meta_raw = rest[:end]
        try:
            meta = yaml.safe_load(meta_raw)
        except yaml.YAMLError:
            return None
        try:
            return FindingRecord(**meta)
        except Exception:  # noqa: BLE001
            return None

    def _serialize(self, rec: FindingRecord) -> str:
        meta = rec.model_dump(mode="json")
        meta_yaml = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).rstrip()
        body = [
            f"# {rec.finding.title}",
            "",
            f"**Endpoint:** `{rec.finding.endpoint}`  **CWE:** {rec.finding.cwe}",
        ]
        if rec.validation:
            body += [
                f"**CVSS 4.0:** {rec.validation.cvss40_score} (`{rec.validation.cvss40_vector}`)",
                "",
                "## Validator verdict",
                rec.validation.verdict,
                "",
                "### Rationale",
                rec.validation.rationale,
                "",
                "### Reproducer",
                "```",
                rec.validation.reproducer,
                "```",
            ]
            if rec.validation.additional_evidence:
                body += ["", "### Additional evidence", rec.validation.additional_evidence]
        body += [
            "",
            "## Summary",
            rec.finding.summary,
            "",
            "## Evidence",
            "```",
            rec.finding.evidence,
            "```",
        ]
        if rec.finding.notes:
            body += ["", "## Notes", rec.finding.notes]
        return f"{_FRONTMATTER_DELIM}{meta_yaml}\n{_FRONTMATTER_DELIM}\n" + "\n".join(body) + "\n"


def new_finding_id() -> str:
    return uuid.uuid4().hex[:10]
