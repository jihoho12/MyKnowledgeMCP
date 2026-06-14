"""SQLite 데이터베이스 관리"""

import json
import sqlite3
import uuid
from pathlib import Path

from knowledge_mcp.config import Config


class Database:
    def __init__(self, config: Config):
        self.config = config
        db_path = Path(config.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        conn = self._get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS nodes (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    category    TEXT NOT NULL DEFAULT 'general',
                    summary     TEXT DEFAULT '',
                    properties  TEXT DEFAULT '{}',
                    is_deleted  INTEGER DEFAULT 0,
                    created_at  TEXT DEFAULT (datetime('now')),
                    updated_at  TEXT DEFAULT (datetime('now')),
                    CHECK(name != '')
                );

                CREATE TABLE IF NOT EXISTS aliases (
                    id          TEXT PRIMARY KEY,
                    node_id     TEXT NOT NULL REFERENCES nodes(id),
                    alias       TEXT NOT NULL,
                    created_at  TEXT DEFAULT (datetime('now')),
                    UNIQUE(alias)
                );

                CREATE TABLE IF NOT EXISTS edges (
                    id              TEXT PRIMARY KEY,
                    source_id       TEXT NOT NULL REFERENCES nodes(id),
                    target_id       TEXT NOT NULL REFERENCES nodes(id),
                    relation        TEXT NOT NULL,
                    description     TEXT DEFAULT '',
                    ingestion_id    TEXT,
                    is_deleted      INTEGER DEFAULT 0,
                    created_at      TEXT DEFAULT (datetime('now')),
                    updated_at      TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS ingestion_log (
                    id          TEXT PRIMARY KEY,
                    raw_text    TEXT NOT NULL,
                    extracted   TEXT NOT NULL,
                    created_at  TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS ingestion_nodes (
                    ingestion_id TEXT NOT NULL REFERENCES ingestion_log(id),
                    node_id      TEXT NOT NULL REFERENCES nodes(id),
                    action       TEXT NOT NULL,
                    PRIMARY KEY (ingestion_id, node_id)
                );

                CREATE TRIGGER IF NOT EXISTS nodes_updated_at
                AFTER UPDATE ON nodes
                FOR EACH ROW
                BEGIN
                    UPDATE nodes SET updated_at = datetime('now')
                    WHERE id = NEW.id AND updated_at = NEW.updated_at;
                END;

                CREATE TRIGGER IF NOT EXISTS edges_updated_at
                AFTER UPDATE ON edges
                FOR EACH ROW
                BEGIN
                    UPDATE edges SET updated_at = datetime('now')
                    WHERE id = NEW.id AND updated_at = NEW.updated_at;
                END;

                CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_name_cat_active
                    ON nodes(name, category) WHERE is_deleted = 0;

                CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
                CREATE INDEX IF NOT EXISTS idx_nodes_category ON nodes(category);
                CREATE INDEX IF NOT EXISTS idx_nodes_not_deleted ON nodes(is_deleted);
                CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
                CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
                CREATE INDEX IF NOT EXISTS idx_edges_not_deleted ON edges(is_deleted);
                CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias);
                CREATE INDEX IF NOT EXISTS idx_aliases_node ON aliases(node_id);
            """)
            conn.commit()
        finally:
            conn.close()

    def find_node_by_name(self, name: str, category: str | None = None) -> dict | None:
        conn = self._get_conn()
        try:
            normalized = name.strip().lower()
            if category:
                row = conn.execute(
                    "SELECT * FROM nodes WHERE LOWER(name) = ? AND category = ? AND is_deleted = 0",
                    (normalized, category),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM nodes WHERE LOWER(name) = ? AND is_deleted = 0",
                    (normalized,),
                ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def find_node_by_alias(self, alias: str) -> dict | None:
        conn = self._get_conn()
        try:
            normalized = alias.strip().lower()
            row = conn.execute(
                """SELECT n.* FROM nodes n
                   JOIN aliases a ON n.id = a.node_id
                   WHERE LOWER(a.alias) = ? AND n.is_deleted = 0""",
                (normalized,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def resolve_node(self, name: str, category: str | None = None) -> dict | None:
        node = self.find_node_by_name(name, category)
        if node:
            return node
        return self.find_node_by_alias(name)

    def get_node_by_id(self, node_id: str) -> dict | None:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM nodes WHERE id = ? AND is_deleted = 0", (node_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_node(self, name: str, category: str, summary: str = "",
                    properties: dict | None = None, aliases: list[str] | None = None) -> dict:
        if not name or not name.strip():
            raise ValueError("Node name cannot be empty")
        node_id = str(uuid.uuid4())
        props_json = json.dumps(properties or {}, ensure_ascii=False)
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO nodes (id, name, category, summary, properties) VALUES (?, ?, ?, ?, ?)",
                (node_id, name, category, summary, props_json),
            )
            for alias in (aliases or []):
                try:
                    conn.execute("INSERT INTO aliases (id, node_id, alias) VALUES (?, ?, ?)",
                                 (str(uuid.uuid4()), node_id, alias))
                except sqlite3.IntegrityError:
                    pass
            conn.commit()
            return self.get_node_by_id(node_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def append_summary(self, node_id: str, new_text: str) -> dict:
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT summary FROM nodes WHERE id = ?", (node_id,)).fetchone()
            if not row:
                raise ValueError(f"Node {node_id} not found")
            existing = row["summary"] or ""
            updated = f"{existing}\n---\n{new_text}" if existing else new_text
            conn.execute("UPDATE nodes SET summary = ? WHERE id = ?", (updated, node_id))
            conn.commit()
            return self.get_node_by_id(node_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_node(self, node_id: str, summary: str | None = None, category: str | None = None,
                    properties: dict | None = None, add_aliases: list[str] | None = None) -> dict:
        conn = self._get_conn()
        try:
            updates, params = [], []
            if summary is not None:
                updates.append("summary = ?")
                params.append(summary)
            if category is not None:
                updates.append("category = ?")
                params.append(category)
            if properties is not None:
                row = conn.execute("SELECT properties FROM nodes WHERE id = ?", (node_id,)).fetchone()
                existing = json.loads(row["properties"]) if row else {}
                existing.update(properties)
                updates.append("properties = ?")
                params.append(json.dumps(existing, ensure_ascii=False))
            if updates:
                params.append(node_id)
                conn.execute(f"UPDATE nodes SET {', '.join(updates)} WHERE id = ?", params)
            for alias in (add_aliases or []):
                try:
                    conn.execute("INSERT INTO aliases (id, node_id, alias) VALUES (?, ?, ?)",
                                 (str(uuid.uuid4()), node_id, alias))
                except sqlite3.IntegrityError:
                    pass
            conn.commit()
            return self.get_node_by_id(node_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_edge(self, source_id: str, target_id: str, relation: str,
                    description: str = "", ingestion_id: str | None = None) -> dict:
        edge_id = str(uuid.uuid4())
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO edges (id, source_id, target_id, relation, description, ingestion_id) VALUES (?, ?, ?, ?, ?, ?)",
                (edge_id, source_id, target_id, relation, description, ingestion_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone()
            return dict(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_edges_for_node(self, node_id: str) -> list[dict]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT e.*, sn.name as source_name, sn.category as source_category,
                          tn.name as target_name, tn.category as target_category
                   FROM edges e
                   JOIN nodes sn ON e.source_id = sn.id
                   JOIN nodes tn ON e.target_id = tn.id
                   WHERE (e.source_id = ? OR e.target_id = ?) AND e.is_deleted = 0""",
                (node_id, node_id),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_aliases_for_node(self, node_id: str) -> list[str]:
        conn = self._get_conn()
        try:
            rows = conn.execute("SELECT alias FROM aliases WHERE node_id = ?", (node_id,)).fetchall()
            return [r["alias"] for r in rows]
        finally:
            conn.close()

    def list_nodes(self, keyword: str | None = None, category: str | None = None,
                   limit: int = 20, offset: int = 0, sort_by: str = "updated_at",
                   sort_order: str = "desc", include_edges: bool = True) -> tuple[list[dict], int]:
        conn = self._get_conn()
        try:
            where_clauses = ["n.is_deleted = 0"]
            params = []
            if keyword:
                where_clauses.append("(LOWER(n.name) LIKE ? OR LOWER(n.summary) LIKE ?)")
                kw = f"%{keyword.strip().lower()}%"
                params.extend([kw, kw])
            if category:
                where_clauses.append("n.category = ?")
                params.append(category)
            where_sql = " AND ".join(where_clauses)
            allowed_sort = {"updated_at", "created_at", "name"}
            if sort_by not in allowed_sort:
                sort_by = "updated_at"
            if sort_order not in {"asc", "desc"}:
                sort_order = "desc"
            count_row = conn.execute(f"SELECT COUNT(*) as cnt FROM nodes n WHERE {where_sql}", params).fetchone()
            total = count_row["cnt"]
            rows = conn.execute(
                f"SELECT n.* FROM nodes n WHERE {where_sql} ORDER BY n.{sort_by} {sort_order} LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            nodes = []
            for row in rows:
                node = dict(row)
                node["aliases"] = self.get_aliases_for_node(node["id"])
                if include_edges:
                    edges = self.get_edges_for_node(node["id"])
                    node["edges"] = [{"id": e["id"], "source": e["source_name"], "target": e["target_name"],
                                      "relation": e["relation"], "description": e["description"]} for e in edges]
                nodes.append(node)
            return nodes, total
        finally:
            conn.close()

    def merge_nodes(self, keep_id: str, absorb_id: str, merged_summary: str | None = None) -> dict:
        conn = self._get_conn()
        try:
            keep_node = dict(conn.execute("SELECT * FROM nodes WHERE id = ? AND is_deleted = 0", (keep_id,)).fetchone())
            absorb_node = dict(conn.execute("SELECT * FROM nodes WHERE id = ? AND is_deleted = 0", (absorb_id,)).fetchone())
            keep_aliases = {r["alias"] for r in conn.execute("SELECT alias FROM aliases WHERE node_id = ?", (keep_id,)).fetchall()}
            absorb_aliases = conn.execute("SELECT id, alias FROM aliases WHERE node_id = ?", (absorb_id,)).fetchall()
            for row in absorb_aliases:
                if row["alias"] in keep_aliases:
                    conn.execute("DELETE FROM aliases WHERE id = ?", (row["id"],))
                else:
                    conn.execute("UPDATE aliases SET node_id = ? WHERE id = ?", (keep_id, row["id"]))
            try:
                conn.execute("INSERT INTO aliases (id, node_id, alias) VALUES (?, ?, ?)",
                             (str(uuid.uuid4()), keep_id, absorb_node["name"]))
            except sqlite3.IntegrityError:
                pass
            conn.execute("UPDATE edges SET source_id = ? WHERE source_id = ? AND is_deleted = 0", (keep_id, absorb_id))
            conn.execute("UPDATE edges SET target_id = ? WHERE target_id = ? AND is_deleted = 0", (keep_id, absorb_id))
            dupes = conn.execute(
                """SELECT source_id, target_id, relation, GROUP_CONCAT(id) as ids,
                          GROUP_CONCAT(description, ' | ') as descriptions, COUNT(*) as cnt
                   FROM edges WHERE (source_id = ? OR target_id = ?) AND is_deleted = 0
                   GROUP BY source_id, target_id, relation HAVING cnt > 1""",
                (keep_id, keep_id),
            ).fetchall()
            edges_deduplicated = 0
            for dupe in dupes:
                ids = dupe["ids"].split(",")
                conn.execute("UPDATE edges SET description = ? WHERE id = ?", (dupe["descriptions"], ids[0]))
                for remove_id in ids[1:]:
                    conn.execute("UPDATE edges SET is_deleted = 1 WHERE id = ?", (remove_id,))
                    edges_deduplicated += 1
            if merged_summary:
                conn.execute("UPDATE nodes SET summary = ? WHERE id = ?", (merged_summary, keep_id))
            else:
                absorb_summary = absorb_node["summary"] or ""
                if absorb_summary:
                    existing = keep_node["summary"] or ""
                    new_summary = f"{existing}\n---\n{absorb_summary}" if existing else absorb_summary
                    conn.execute("UPDATE nodes SET summary = ? WHERE id = ?", (new_summary, keep_id))
            conn.execute("UPDATE nodes SET is_deleted = 1 WHERE id = ?", (absorb_id,))
            conn.commit()
            edges_migrated = conn.execute(
                "SELECT COUNT(*) as cnt FROM edges WHERE (source_id = ? OR target_id = ?) AND is_deleted = 0",
                (keep_id, keep_id),
            ).fetchone()["cnt"]
            return {"merged_node": dict(conn.execute("SELECT * FROM nodes WHERE id = ?", (keep_id,)).fetchone()),
                    "edges_migrated": edges_migrated, "edges_deduplicated": edges_deduplicated,
                    "absorbed_node": absorb_node["name"]}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def soft_delete_node(self, node_id: str, cascade: bool = False) -> dict:
        conn = self._get_conn()
        try:
            edge_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM edges WHERE (source_id = ? OR target_id = ?) AND is_deleted = 0",
                (node_id, node_id),
            ).fetchone()["cnt"]
            if edge_count > 0 and not cascade:
                return {"deleted": False, "edge_count": edge_count}
            deleted_edges = 0
            if cascade and edge_count > 0:
                conn.execute("UPDATE edges SET is_deleted = 1 WHERE (source_id = ? OR target_id = ?) AND is_deleted = 0",
                             (node_id, node_id))
                deleted_edges = edge_count
            conn.execute("UPDATE nodes SET is_deleted = 1 WHERE id = ?", (node_id,))
            conn.commit()
            return {"deleted": True, "deleted_edges": deleted_edges}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def soft_delete_edge_by_id(self, edge_id: str) -> bool:
        conn = self._get_conn()
        try:
            result = conn.execute("UPDATE edges SET is_deleted = 1 WHERE id = ? AND is_deleted = 0", (edge_id,))
            conn.commit()
            return result.rowcount > 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def soft_delete_edges_by_condition(self, source_name: str, target_name: str, relation: str) -> int:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT e.id FROM edges e
                   JOIN nodes sn ON e.source_id = sn.id JOIN nodes tn ON e.target_id = tn.id
                   WHERE LOWER(sn.name) = ? AND LOWER(tn.name) = ? AND e.relation = ? AND e.is_deleted = 0""",
                (source_name.strip().lower(), target_name.strip().lower(), relation),
            ).fetchall()
            count = 0
            for row in rows:
                conn.execute("UPDATE edges SET is_deleted = 1 WHERE id = ?", (row["id"],))
                count += 1
            conn.commit()
            return count
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_ingestion_log(self, raw_text: str, extracted: dict) -> str:
        log_id = str(uuid.uuid4())
        conn = self._get_conn()
        try:
            conn.execute("INSERT INTO ingestion_log (id, raw_text, extracted) VALUES (?, ?, ?)",
                         (log_id, raw_text, json.dumps(extracted, ensure_ascii=False)))
            conn.commit()
            return log_id
        finally:
            conn.close()

    def create_ingestion_node(self, ingestion_id: str, node_id: str, action: str):
        conn = self._get_conn()
        try:
            conn.execute("INSERT OR IGNORE INTO ingestion_nodes (ingestion_id, node_id, action) VALUES (?, ?, ?)",
                         (ingestion_id, node_id, action))
            conn.commit()
        finally:
            conn.close()
