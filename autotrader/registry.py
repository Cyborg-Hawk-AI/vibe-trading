"""SQLite strategy registry: genomes, lineage, per-window results, gate status.

Single file at ``autotrader/state/registry.db``. Everything the outer loop
learns is persisted here so runs are resumable and the whole search history
is auditable (and syncable to S3).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from autotrader.bootstrap import state_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    gid TEXT NOT NULL,
    sleeve TEXT NOT NULL,
    generation INTEGER NOT NULL,
    parent_gid TEXT,
    genome_json TEXT NOT NULL,
    origin TEXT NOT NULL,            -- seed | mutation | biased_mutation | crossover | llm_proposal
    created_at REAL NOT NULL,
    PRIMARY KEY (gid, sleeve)
);
CREATE TABLE IF NOT EXISTS results (
    gid TEXT NOT NULL,
    sleeve TEXT NOT NULL,
    generation INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT,
    total_return REAL,
    max_drawdown REAL,
    sharpe REAL,
    trade_count INTEGER,
    win_rate REAL,
    train_pass_rate REAL,
    oos_pass_rate REAL,
    train_mean_window_ret REAL,
    train_sharpe REAL,
    oos_mean_window_ret REAL,
    gate_passed INTEGER,
    windows_json TEXT,
    diagnostics_json TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (gid, sleeve)
);
CREATE TABLE IF NOT EXISTS generations (
    sleeve TEXT NOT NULL,
    generation INTEGER NOT NULL,
    best_gid TEXT,
    best_fitness_json TEXT,
    failure_counts_json TEXT,
    llm_memo TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (sleeve, generation)
);
CREATE TABLE IF NOT EXISTS committee_reviews (
    gid TEXT NOT NULL,
    sleeve TEXT NOT NULL,
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    decision TEXT,
    report_json TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (gid, sleeve, ticker, trade_date)
);
"""


class Registry:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else state_dir() / "registry.db"
        self._conn = sqlite3.connect(str(self.path))
        self._conn.executescript(_SCHEMA)

        columns = {
            row[1]
            for row in self._conn.execute(
                "PRAGMA table_info(results)"
            )
        }

        if "train_sharpe" not in columns:
            self._conn.execute(
                "ALTER TABLE results ADD COLUMN train_sharpe REAL"
            )

        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- strategies ---------------------------------------------------------

    def record_strategy(
        self,
        gid: str,
        sleeve: str,
        generation: int,
        genome: dict[str, Any],
        origin: str,
        parent_gid: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO strategies VALUES (?,?,?,?,?,?,?)",
            (gid, sleeve, generation, parent_gid, json.dumps(genome, sort_keys=True),
             origin, time.time()),
        )
        self._conn.commit()

    # -- results ------------------------------------------------------------
    def record_result(
        self,
        sim_result: Any,
        generation: int,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        r = sim_result
        self._conn.execute(
            """
            INSERT OR REPLACE INTO results (
                gid, sleeve, generation, ok, error,
                total_return, max_drawdown, sharpe,
                trade_count, win_rate,
                train_pass_rate, oos_pass_rate,
                train_mean_window_ret, train_sharpe,
                oos_mean_window_ret, gate_passed,
                windows_json, diagnostics_json,
                created_at
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?
            )
            """,
            (
                r.gid, r.sleeve, generation,
                int(r.ok), r.error,
                r.total_return, r.max_drawdown,
                r.sharpe, r.trade_count, r.win_rate,
                r.train_pass_rate, r.oos_pass_rate,
                r.train_mean_window_ret,
                r.train_sharpe,
                r.oos_mean_window_ret,
                int(r.gate_passed),
                json.dumps([vars(w) for w in r.windows]),
                json.dumps(diagnostics or {}),
                time.time(),
            ),
        )
        self._conn.commit()
        
    def record_generation(
        self,
        sleeve: str,
        generation: int,
        best_gid: str,
        best_fitness: tuple,
        failure_counts: dict[str, int],
        llm_memo: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO generations VALUES (?,?,?,?,?,?,?)",
            (sleeve, generation, best_gid, json.dumps(list(best_fitness)),
             json.dumps(failure_counts), llm_memo, time.time()),
        )
        self._conn.commit()

    def record_committee_review(
        self, gid: str, sleeve: str, ticker: str, trade_date: str,
        decision: str, report: dict[str, Any],
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO committee_reviews VALUES (?,?,?,?,?,?,?)",
            (gid, sleeve, ticker, trade_date, decision, json.dumps(report), time.time()),
        )
        self._conn.commit()

    # -- queries ------------------------------------------------------------

    def research_leaderboard(
        self, sleeve: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT r.gid, r.generation,
                   r.train_pass_rate,
                   r.train_mean_window_ret,
                   r.train_sharpe,
                   s.genome_json
            FROM results r JOIN strategies s
              ON r.gid = s.gid AND r.sleeve = s.sleeve
            WHERE r.sleeve = ?
              AND r.ok = 1
              AND r.train_sharpe IS NOT NULL
            ORDER BY r.train_pass_rate DESC,
                     r.train_mean_window_ret DESC,
                     r.train_sharpe DESC,
                     r.gid ASC
            LIMIT ?
            """,
            (sleeve, limit),
        )
        cols = [d[0] for d in cur.description]
        rows = [
            dict(zip(cols, row))
            for row in cur.fetchall()
        ]
        for row in rows:
            row["genome"] = json.loads(
                row.pop("genome_json")
            )
        return rows    
    def leaderboard(self, sleeve: str, limit: int = 10) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT r.gid, r.generation, r.oos_pass_rate, r.train_pass_rate,
                   r.oos_mean_window_ret, r.total_return, r.max_drawdown,
                   r.sharpe, r.gate_passed, s.genome_json
            FROM results r JOIN strategies s
              ON r.gid = s.gid AND r.sleeve = s.sleeve
            WHERE r.sleeve = ? AND r.ok = 1
            ORDER BY r.gate_passed DESC, r.oos_pass_rate DESC,
                     r.oos_mean_window_ret DESC, r.sharpe DESC
            LIMIT ?
            """,
            (sleeve, limit),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for row in rows:
            row["genome"] = json.loads(row.pop("genome_json"))
        return rows

    def gate_passers(self, sleeve: str) -> list[dict[str, Any]]:
        return [r for r in self.leaderboard(sleeve, limit=1000) if r["gate_passed"]]

    def export_json(self, path: str | Path) -> None:
        """Dump leaderboards + generation history for the S3 artifact sync."""
        out = {
            "exported_at": time.time(),
            "leaderboards": {s: self.leaderboard(s, 20) for s in ("equities", "crypto")},
            "generations": [],
        }
        cur = self._conn.execute(
            "SELECT sleeve, generation, best_gid, best_fitness_json,"
            " failure_counts_json, llm_memo FROM generations"
            " ORDER BY sleeve, generation"
        )
        for sleeve, gen, gid, fit, fails, memo in cur.fetchall():
            out["generations"].append(
                {
                    "sleeve": sleeve,
                    "generation": gen,
                    "best_gid": gid,
                    "best_fitness": json.loads(fit or "[]"),
                    "failure_counts": json.loads(fails or "{}"),
                    "llm_memo": memo,
                }
            )
        Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")
