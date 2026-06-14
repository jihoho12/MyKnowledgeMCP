# Knowledge MCP

배운 CS 지식을 **그래프 구조**로 자동 정리·병합하는 MCP 서버입니다.

Claude Desktop에서 자연어로 지식을 말하면, 핵심 개념(Entity)과 관계(Relation)를 추출해서 SQLite 기반 지식 그래프에 저장합니다. 같은 개념이 다시 나오면 기존 노드에 병합합니다.

## 해결하는 문제

- **지식 파편화**: 같은 개념에 대해 시차를 두고 배운 정보가 하나의 노드에 누적
- **컨텍스트 비대화**: 전체 대화를 넘기지 않고, 관련 노드만 꺼내서 토큰 절약
- **할루시네이션 방지**: 정제된 지식 베이스를 기반으로 LLM 답변 품질 향상

## 아키텍처

```
사용자 → Claude(Entity/Relation 추출) → MCP(저장) → SQLite(WAL 모드)
```

MCP 내부에 별도 LLM이 없습니다. Claude가 추출과 판단을, MCP가 저장과 조회를 담당합니다.

## 도구

| 도구 | 설명 |
|------|------|
| `ingest_knowledge` | Entity/Relation JSON을 받아 그래프에 upsert |
| `list_nodes` | 키워드 검색, 카테고리 필터, 페이지네이션 |
| `merge_nodes` | 중복 노드 병합 (alias 이전, edge 중복 처리) |
| `update_node` | 노드 속성 직접 수정 |
| `delete_node` | 노드 soft delete (cascade 옵션) |
| `delete_edge` | 관계 soft delete (ID 또는 조건) |

## 설치

```bash
git clone https://github.com/jihoho12/MyKnowledgeMCP.git
cd MyKnowledgeMCP
uv sync
```

## Claude Desktop 연동

`claude_desktop_config.json`에 추가:

```json
{
  "mcpServers": {
    "knowledge": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/MyKnowledgeMCP", "python", "-m", "knowledge_mcp.server"]
    }
  }
}
```

## 사용 예시

Claude Desktop에서 자연어로:

```
"HTTP는 TCP 위에서 동작하는 애플리케이션 계층 프로토콜이야. 정리해줘."
→ Claude가 Entity/Relation 추출 → ingest_knowledge 호출

"네트워크 관련 지식 보여줘"
→ list_nodes(category="network")

"'서버'랑 'Server' 같은 개념이니까 합쳐줘"
→ merge_nodes(node_a="서버", node_b="Server")
```

## 기술 스택

| 구성 요소 | 선택 |
|-----------|------|
| MCP 서버 | Python + FastMCP |
| 저장소 | SQLite (WAL 모드) |
| 패키지 관리 | uv |
| Entity 추출 | Claude (대화 중인 LLM) |

## 설정

`config.toml`에서 카테고리 목록, DB 경로, summary 통합 임계값을 변경할 수 있습니다.

```toml
[knowledge]
categories = ["general", "network", "os", "database", "web", "security", "language", "algorithm", "architecture", "devops"]
summary_consolidation_threshold = 500
```

## 테스트

48개 테스트 전체 통과 (기능 28 + 고급 20)

- 도구별 정상/에러 경로
- 동시성 (WAL 멀티스레드)
- 대량 데이터 성능 (500 노드)
- 데이터 무결성 (SQL injection, 특수문자, 외래키)
- 복구 (DB 손상, 백업/복원)

## 라이선스

MIT
