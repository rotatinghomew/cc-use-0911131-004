"""vintages 职责（二）：只增不改的 SQLite 版本存储。

表设计遵循“原发布和链式历史不得被覆盖”：

* restatement_runs       一次分析运行的输入指纹与输出摘要；
* restatement_vintages   每个系统时点（原发布/重述）一行，冻结所用来源版本；
* restatement_periods    逐期水平、匹配链、可比状态；
* restatement_contributions  逐笔桥接贡献，含 observation_id / mapping_id 追溯路径；
* restatement_evidence   原报价版本与映射版本的冻结快照。

所有业务表按业务主键 INSERT OR IGNORE / 冲突即报错，重复运行幂等但绝不更新
历史行；原发布 publication 的锚定水平单独保存以便对账。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from market_index_bureau.restatement.domain import Contribution
from market_index_bureau.vintages.index_store import fingerprint, stable_json, utc_now


class RestatementStore:
    def __init__(self, database: str | Path):
        self.database = str(database)
        self._initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS restatement_runs(
                    run_id TEXT PRIMARY KEY,
                    input_fingerprint TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    output TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS restatement_vintages(
                    vintage_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    base_period TEXT NOT NULL,
                    anchor_period TEXT,
                    anchor_level REAL,
                    source_versions TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS restatement_periods(
                    vintage_key TEXT NOT NULL,
                    period TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    basis TEXT NOT NULL,
                    comparable INTEGER NOT NULL,
                    headline_level REAL NOT NULL,
                    matched_index REAL,
                    match_weight_coverage REAL NOT NULL,
                    match_key_count INTEGER NOT NULL,
                    basket_size INTEGER NOT NULL,
                    entrant_count INTEGER NOT NULL,
                    flags TEXT NOT NULL,
                    PRIMARY KEY (vintage_key, period)
                );
                CREATE TABLE IF NOT EXISTS restatement_contributions(
                    contribution_id TEXT PRIMARY KEY,
                    vintage_key TEXT NOT NULL,
                    period TEXT NOT NULL,
                    factor TEXT NOT NULL,
                    amount REAL NOT NULL,
                    observation_ids TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS restatement_evidence(
                    vintage_key TEXT NOT NULL,
                    observation_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    quote_snapshot TEXT NOT NULL,
                    mapping_id TEXT,
                    PRIMARY KEY (vintage_key, observation_id)
                );
                CREATE INDEX IF NOT EXISTS idx_contrib_period
                    ON restatement_contributions(vintage_key, period);
                """
            )

    def recall_run(self, input_fingerprint: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT output FROM restatement_runs WHERE input_fingerprint=?",
                (input_fingerprint,),
            ).fetchone()
        if row is None:
            return None
        import json

        return json.loads(row["output"])

    def save_run(self, run_id: str, input_fingerprint: str, as_of: str, output: Mapping[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO restatement_runs(run_id,input_fingerprint,as_of,output,created_at) "
                "VALUES(?,?,?,?,?)",
                (run_id, input_fingerprint, as_of, stable_json(output), utc_now()),
            )

    def save_vintage(
        self,
        run_id: str,
        vintage_key: str,
        kind: str,
        as_of: str,
        base_period: str,
        anchor_period: str | None,
        anchor_level: float | None,
        source_versions: Mapping[str, Any],
        period_rows: list[Mapping[str, Any]],
        contributions: list[Contribution],
        evidence_rows: list[Mapping[str, Any]],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO restatement_vintages"
                "(vintage_key,run_id,kind,as_of,base_period,anchor_period,anchor_level,source_versions,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    vintage_key,
                    run_id,
                    kind,
                    as_of,
                    base_period,
                    anchor_period,
                    anchor_level,
                    stable_json(source_versions),
                    utc_now(),
                ),
            )
            for row in period_rows:
                connection.execute(
                    "INSERT OR IGNORE INTO restatement_periods"
                    "(vintage_key,period,kind,basis,comparable,headline_level,matched_index,"
                    "match_weight_coverage,match_key_count,basket_size,entrant_count,flags) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        vintage_key,
                        row["period"],
                        kind,
                        row["basis"],
                        1 if row["comparable"] else 0,
                        row["headline_level"],
                        row["matched_index"],
                        row["match_weight_coverage"],
                        row["match_key_count"],
                        row["basket_size"],
                        row["entrant_count"],
                        stable_json(row["flags"]),
                    ),
                )
            for contribution in contributions:
                contribution_id = fingerprint(
                    [vintage_key, contribution.period, contribution.factor, contribution.observation_ids]
                )
                connection.execute(
                    "INSERT OR IGNORE INTO restatement_contributions"
                    "(contribution_id,vintage_key,period,factor,amount,observation_ids,evidence,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        contribution_id,
                        vintage_key,
                        contribution.period,
                        contribution.factor,
                        contribution.amount,
                        stable_json(list(contribution.observation_ids)),
                        stable_json(contribution.evidence),
                        utc_now(),
                    ),
                )
            for evidence in evidence_rows:
                connection.execute(
                    "INSERT OR IGNORE INTO restatement_evidence"
                    "(vintage_key,observation_id,revision,quote_snapshot,mapping_id) "
                    "VALUES(?,?,?,?,?)",
                    (
                        vintage_key,
                        evidence["observation_id"],
                        evidence["revision"],
                        stable_json(evidence["quote_snapshot"]),
                        evidence.get("mapping_id"),
                    ),
                )

    def stats(self) -> dict[str, int]:
        with self.connect() as connection:
            return {
                name: int(
                    connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                )
                for name in (
                    "restatement_runs",
                    "restatement_vintages",
                    "restatement_periods",
                    "restatement_contributions",
                    "restatement_evidence",
                )
            }
