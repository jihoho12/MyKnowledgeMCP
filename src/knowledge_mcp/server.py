"""Knowledge Ingestion MCP Server

Claude가 추출한 Entity/Relation을 받아서 지식 그래프에 저장하는 MCP 서버입니다.
"""

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from knowledge_mcp.config import Config, load_config
from knowledge_mcp.database import Database

mcp = FastMCP(
    "knowledge",
    instructions="""지식 그래프 저장소입니다. 사용자가 학습한 내용을 Entity/Relation으로 추출한 뒤,
이 MCP의 도구를 사용하여 그래프에 저장·조회·병합·삭제할 수 있습니다.

일반적인 사용 흐름:
1. list_nodes로 기존 지식 확인
2. 사용자의 텍스트에서 Entity/Relation을 추출 (Claude가 수행)
3. ingest_knowledge로 저장
4. 필요 시 merge_nodes, update_node, delete_node, delete_edge로 관리""",
)

config = load_config()
db_path = os.environ.get("KNOWLEDGE_DB_PATH", config.db_path)
if not Path(db_path).is_absolute():
    project_root = Path(__file__).parent.parent.parent
    db_path = str(project_root / db_path)
config.db_path = db_path
db = Database(config)


def _error(code: str, message: str) -> str:
    return json.dumps({"success": False, "error": {"code": code, "message": message}}, ensure_ascii=False, indent=2)


def _success(data: dict) -> str:
    return json.dumps({"success": True, "data": data}, ensure_ascii=False, indent=2)


@mcp.tool()
def ingest_knowledge(raw_text: str, entities: str, relations: str) -> str:
    """지식을 그래프에 저장합니다. Claude가 추출한 Entity/Relation JSON을 받습니다.

    Args:
        raw_text: 사용자가 입력한 원본 텍스트 (로그용)
        entities: JSON 배열. 예: [{"name": "서버", "category": "network", "description": "클라이언트에게 서비스를 제공하는 컴퓨터", "aliases": ["Server"], "properties": {}}]
        relations: JSON 배열. 예: [{"source": "서버", "target": "클라이언트", "relation": "serves", "description": "서비스 제공 관계"}]
    """
    try:
        entity_list = json.loads(entities)
        relation_list = json.loads(relations)
    except json.JSONDecodeError as e:
        return _error("INVALID_INPUT", f"JSON 파싱 실패: {e}")

    if not entity_list:
        return _error("INVALID_INPUT", "entities 배열이 비어있습니다")

    for ent in entity_list:
        cat = ent.get("category", "general")
        if cat not in config.categories:
            return _error("INVALID_CATEGORY", f"허용되지 않은 카테고리: '{cat}'. 허용 목록: {config.categories}")

    created_nodes, updated_nodes, new_edges, consolidation_needed = [], [], [], []
    node_name_to_id = {}

    ingestion_id = db.create_ingestion_log(raw_text=raw_text, extracted={"entities": entity_list, "relations": relation_list})

    for ent in entity_list:
        name = ent.get("name", "").strip()
        category = ent.get("category", "general")
        description = ent.get("description", "")
        aliases = ent.get("aliases", [])
        properties = ent.get("properties", {})
        if not name:
            continue

        existing = db.resolve_node(name, category)
        if existing:
            if description:
                updated = db.append_summary(existing["id"], description)
                if properties:
                    db.update_node(existing["id"], properties=properties)
                if aliases:
                    db.update_node(existing["id"], add_aliases=aliases)
                updated_nodes.append(existing["name"])
                db.create_ingestion_node(ingestion_id, existing["id"], "updated")
                if len(updated.get("summary", "")) > config.summary_consolidation_threshold:
                    consolidation_needed.append(existing["name"])
            else:
                db.create_ingestion_node(ingestion_id, existing["id"], "updated")
            node_name_to_id[name] = existing["id"]
            node_name_to_id[existing["name"]] = existing["id"]
        else:
            node = db.create_node(name=name, category=category, summary=description, properties=properties, aliases=aliases)
            created_nodes.append(name)
            node_name_to_id[name] = node["id"]
            db.create_ingestion_node(ingestion_id, node["id"], "created")

    for rel in relation_list:
        source_name = rel.get("source", "").strip()
        target_name = rel.get("target", "").strip()
        relation = rel.get("relation", "").strip()
        description = rel.get("description", "")
        if not (source_name and target_name and relation):
            continue
        source_id = node_name_to_id.get(source_name)
        if not source_id:
            source_node = db.resolve_node(source_name)
            source_id = source_node["id"] if source_node else None
        target_id = node_name_to_id.get(target_name)
        if not target_id:
            target_node = db.resolve_node(target_name)
            target_id = target_node["id"] if target_node else None
        if not source_id or not target_id:
            continue
        db.create_edge(source_id=source_id, target_id=target_id, relation=relation, description=description, ingestion_id=ingestion_id)
        new_edges.append({"source": source_name, "target": target_name, "relation": relation})

    result = {"created_nodes": created_nodes, "updated_nodes": updated_nodes, "new_edges": new_edges, "ingestion_id": ingestion_id}
    if consolidation_needed:
        result["consolidation_needed"] = consolidation_needed
    return _success(result)


@mcp.tool()
def list_nodes(keyword: str = "", category: str = "", include_edges: bool = True,
              limit: int = 20, offset: int = 0, sort_by: str = "updated_at", sort_order: str = "desc") -> str:
    """지식 그래프의 노드 목록을 조회합니다.

    Args:
        keyword: 검색어 (name, summary에서 부분 매칭). 빈 문자열이면 전체 조회.
        category: 카테고리 필터. 빈 문자열이면 전체.
        include_edges: 각 노드의 연결된 edge 포함 여부
        limit: 최대 반환 수 (기본 20, 최대 100)
        offset: 페이지네이션 오프셋
        sort_by: 정렬 기준 (updated_at, created_at, name)
        sort_order: 정렬 방향 (asc, desc)
    """
    limit = min(max(1, limit), 100)
    offset = max(0, offset)
    try:
        nodes, total = db.list_nodes(keyword=keyword or None, category=category or None,
                                     limit=limit, offset=offset, sort_by=sort_by,
                                     sort_order=sort_order, include_edges=include_edges)
        cleaned = [{"id": n["id"], "name": n["name"], "category": n["category"], "summary": n["summary"],
                    "aliases": n.get("aliases", []), "edges": n.get("edges", []),
                    "updated_at": n["updated_at"], "created_at": n["created_at"]} for n in nodes]
        return _success({"nodes": cleaned, "total_count": total, "has_more": (offset + limit) < total})
    except Exception as e:
        return _error("DB_ERROR", str(e))


@mcp.tool()
def merge_nodes(node_a: str, node_b: str, merged_summary: str = "") -> str:
    """두 노드를 하나로 병합합니다. node_a를 유지하고 node_b를 흡수합니다.

    Args:
        node_a: 유지할 노드 이름
        node_b: 흡수될 노드 이름 (병합 후 soft delete)
        merged_summary: Claude가 합쳐서 작성한 통합 요약 (선택. 비어있으면 단순 append)
    """
    keep = db.resolve_node(node_a)
    if not keep:
        return _error("NODE_NOT_FOUND", f"유지할 노드 '{node_a}'를 찾을 수 없습니다")
    absorb = db.resolve_node(node_b)
    if not absorb:
        return _error("NODE_NOT_FOUND", f"흡수할 노드 '{node_b}'를 찾을 수 없습니다")
    if keep["id"] == absorb["id"]:
        return _error("INVALID_INPUT", "같은 노드를 병합할 수 없습니다")
    try:
        result = db.merge_nodes(keep_id=keep["id"], absorb_id=absorb["id"], merged_summary=merged_summary or None)
        return _success(result)
    except Exception as e:
        return _error("DB_ERROR", str(e))


@mcp.tool()
def update_node(name: str, summary: str = "", category: str = "", add_aliases: str = "", properties: str = "") -> str:
    """특정 노드의 속성을 수정합니다.

    Args:
        name: 수정할 노드 이름
        summary: 새 요약 (전체 교체). 빈 문자열이면 변경 안 함.
        category: 새 카테고리. 빈 문자열이면 변경 안 함.
        add_aliases: 추가할 별칭 JSON 배열. 예: ["웹서버", "web server"]. 빈 문자열이면 변경 안 함.
        properties: 추가할 속성 JSON 객체. 기존 속성에 merge. 빈 문자열이면 변경 안 함.
    """
    node = db.resolve_node(name)
    if not node:
        return _error("NODE_NOT_FOUND", f"노드 '{name}'를 찾을 수 없습니다")
    if category and category not in config.categories:
        return _error("INVALID_CATEGORY", f"허용되지 않은 카테고리: '{category}'. 허용 목록: {config.categories}")
    parsed_aliases = None
    if add_aliases:
        try:
            parsed_aliases = json.loads(add_aliases)
        except json.JSONDecodeError:
            return _error("INVALID_INPUT", "add_aliases JSON 파싱 실패")
    parsed_props = None
    if properties:
        try:
            parsed_props = json.loads(properties)
        except json.JSONDecodeError:
            return _error("INVALID_INPUT", "properties JSON 파싱 실패")
    try:
        updated = db.update_node(node_id=node["id"], summary=summary or None, category=category or None,
                                 properties=parsed_props, add_aliases=parsed_aliases)
        return _success({"updated_node": {"id": updated["id"], "name": updated["name"],
                                           "category": updated["category"], "summary": updated["summary"],
                                           "updated_at": updated["updated_at"]}})
    except Exception as e:
        return _error("DB_ERROR", str(e))


@mcp.tool()
def delete_node(name: str, cascade: bool = False) -> str:
    """노드를 삭제(soft delete)합니다.

    Args:
        name: 삭제할 노드 이름
        cascade: True면 연결된 edge도 함께 삭제. False면 edge가 있을 경우 거부.
    """
    node = db.resolve_node(name)
    if not node:
        return _error("NODE_NOT_FOUND", f"노드 '{name}'를 찾을 수 없습니다")
    try:
        result = db.soft_delete_node(node["id"], cascade=cascade)
        if not result["deleted"]:
            return _error("EDGE_EXISTS", f"'{name}'에 연결된 edge가 {result['edge_count']}개 있습니다. cascade=true로 재시도하세요.")
        return _success({"deleted_node": name, "deleted_edges": result["deleted_edges"]})
    except Exception as e:
        return _error("DB_ERROR", str(e))


@mcp.tool()
def delete_edge(edge_id: str = "", source: str = "", target: str = "", relation: str = "") -> str:
    """edge를 삭제(soft delete)합니다. edge_id로 직접 삭제하거나, source/target/relation 조건으로 삭제합니다.

    Args:
        edge_id: 삭제할 edge ID (이 값이 있으면 다른 조건 무시)
        source: source 노드 이름 (조건 삭제용)
        target: target 노드 이름 (조건 삭제용)
        relation: 관계 타입 (조건 삭제용)
    """
    if edge_id:
        try:
            deleted = db.soft_delete_edge_by_id(edge_id)
            if not deleted:
                return _error("EDGE_NOT_FOUND", f"Edge '{edge_id}'를 찾을 수 없습니다")
            return _success({"deleted_count": 1})
        except Exception as e:
            return _error("DB_ERROR", str(e))
    elif source and target and relation:
        try:
            count = db.soft_delete_edges_by_condition(source, target, relation)
            if count == 0:
                return _error("EDGE_NOT_FOUND", f"'{source}' → '{target}' [{relation}] 조건에 맞는 edge가 없습니다")
            return _success({"deleted_count": count})
        except Exception as e:
            return _error("DB_ERROR", str(e))
    else:
        return _error("INVALID_INPUT", "edge_id를 지정하거나, source + target + relation을 모두 지정해주세요.")


def main():
    mcp.run()


if __name__ == "__main__":
    main()
