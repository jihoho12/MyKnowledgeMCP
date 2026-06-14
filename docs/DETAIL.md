# Knowledge MCP — 상세 문서

## 1. 아키텍처

### 전체 흐름

```
사용자 → Claude(추출) → MCP(저장) → SQLite
```

Claude가 자연어에서 Entity/Relation을 추출하고, MCP는 이를 받아 DB에 upsert합니다.
MCP 내부에 별도 LLM이 없으며, 추출과 판단은 전적으로 Claude의 역할입니다.

### 역할 분리

| 역할 | 담당 |
|------|------|
| 텍스트에서 Entity/Relation 추출 | Claude |
| 기존 노드와 동일한지 판단 | Claude (`list_nodes`로 기존 그래프 확인 후 판단) |
| summary 모순 감지 및 통합 | Claude (`consolidation_needed` 플래그 확인 후 `update_node`로 재작성) |
| 구조화된 데이터의 저장/조회/삭제 | MCP 서버 |

### 기술 스택

| 구성 요소 | 선택 | 비고 |
|-----------|------|------|
| MCP 서버 | Python + FastMCP (`mcp` SDK) | stdio 전송 방식 |
| 패키지 관리 | uv | 가상환경 자동 관리 |
| 저장소 | SQLite (WAL 모드) | 동시 읽기 지원, 파일 기반 |
| Entity 추출 | Claude | 별도 LLM 불필요 |

## 2. 데이터 모델

### 테이블 구조

**nodes** — 핵심 개념 저장

| 컨럼 | 타입 | 설명 |
|------|------|------|
| id | TEXT PK | UUID |
| name | TEXT NOT NULL | 개념 이름 ("서버", "HTTP") |
| category | TEXT NOT NULL | 카테고리 ("network", "os" 등) |
| summary | TEXT | 누적된 설명 |
| properties | TEXT (JSON) | 추가 속성 |
| is_deleted | INTEGER | 0=활성, 1=삭제 |
| created_at | TEXT | 생성 시각 |
| updated_at | TEXT | 수정 시각 (트리거로 자동 갱신) |

- `(name, category)` 조합이 활성 노드 내에서 유니크 (부분 인덱스: `WHERE is_deleted = 0`)
- soft delete 후 같은 이름으로 재생성 가능

**aliases** — 별칭 매핑

| 컨럼 | 타입 | 설명 |
|------|------|------|
| id | TEXT PK | UUID |
| node_id | TEXT FK → nodes | 매핑된 노드 |
| alias | TEXT UNIQUE | "Server", "웹서버" 등 |

**edges** — 개념 간 관계

| 컨럼 | 타입 | 설명 |
|------|------|------|
| id | TEXT PK | UUID |
| source_id | TEXT FK → nodes | 출발 노드 |
| target_id | TEXT FK → nodes | 도착 노드 |
| relation | TEXT | "is_a", "uses", "runs_on" 등 |
| description | TEXT | 관계 부연 설명 |
| ingestion_id | TEXT FK → ingestion_log | 출처 추적 |
| is_deleted | INTEGER | soft delete |

**ingestion_log** — 입력 이력

| 컨럼 | 타입 | 설명 |
|------|------|------|
| id | TEXT PK | UUID |
| raw_text | TEXT | 사용자 원본 텍스트 |
| extracted | TEXT (JSON) | Claude가 추출한 결과 |
| created_at | TEXT | 입력 시각 |

**ingestion_nodes** — 입력-노드 연결 (중간 테이블)

| 컨럼 | 타입 | 설명 |
|------|------|------|
| ingestion_id | TEXT FK | 입력 기록 |
| node_id | TEXT FK | 영향받은 노드 |
| action | TEXT | "created" 또는 "updated" |

### 노드 매칭 순서

지식 입력 시 기존 노드와의 매칭은 다음 순서로 진행됩니다:

1. **(name, category) 정확 매칭** — 대소문자 무시 (LOWER 적용)
2. **aliases 테이블 매칭** — 별칭으로 등록된 이름과 비교
3. **매칭 실패 시 새 노드 생성**

### summary 병합 규칙

- 기존 summary에 `\n---\n` 구분선으로 이어붙이기 (append)
- summary가 500자를 초과하면 `consolidation_needed` 플래그 반환
- Claude가 플래그를 보고 `update_node`로 통합 요약을 작성 (consolidation은 Claude의 일)
- 모순 판단도 Claude의 몷 (MCP는 텍스트의 의미를 판단하지 않음)

## 3. 도구 명세

### 공통 응답 형식

```json
// 성공
{ "success": true, "data": { ... } }

// 실패
{ "success": false, "error": { "code": "ERROR_CODE", "message": "설명" } }
```

### 에러 코드

| 코드 | 의미 |
|------|------|
| NODE_NOT_FOUND | 지정한 노드가 없음 |
| EDGE_NOT_FOUND | 지정한 edge가 없음 |
| EDGE_EXISTS | 삭제 거부 (연결된 edge 존재, cascade 필요) |
| DUPLICATE_NODE | 같은 (name, category) 노드가 이미 존재 |
| INVALID_INPUT | 필수 필드 누락 또는 형식 오류 |
| INVALID_CATEGORY | 허용되지 않은 카테고리 |
| DB_ERROR | SQLite 쓰기 실패, 락 충돌 등 |

### 3-1. ingest_knowledge

지식을 그래프에 저장합니다.

**입력:**
- `raw_text` (string, 필수): 사용자 원본 텍스트
- `entities` (string, 필수): Entity JSON 배열
  ```json
  [{"name": "서버", "category": "network", "description": "설명", "aliases": ["Server"], "properties": {}}]
  ```
- `relations` (string, 필수): Relation JSON 배열
  ```json
  [{"source": "서버", "target": "클라이언트", "relation": "serves", "description": "설명"}]
  ```

**처리 과정:**
1. 각 entity에 대해 기존 노드 매칭 (name → alias → 새 생성)
2. 매칭 시 summary append, properties merge, aliases 추가
3. 각 relation에 대해 edge 생성
4. ingestion_log + ingestion_nodes 기록

**출력:** `created_nodes`, `updated_nodes`, `new_edges`, `ingestion_id`, `consolidation_needed`(해당 시)

### 3-2. list_nodes

노드 목록을 조회합니다.

**입력:**
- `keyword` (string): name/summary 부분 매칭 검색어
- `category` (string): 카테고리 필터
- `include_edges` (bool, 기본 true): 연결된 edge 포함 여부
- `limit` (int, 기본 20, 최대 100): 반환 수
- `offset` (int, 기본 0): 페이지네이션
- `sort_by` (string): "updated_at" | "created_at" | "name"
- `sort_order` (string): "asc" | "desc"

**출력:** `nodes`, `total_count`, `has_more`

### 3-3. merge_nodes

두 노드를 하나로 병합합니다.

**입력:**
- `node_a` (string, 필수): 유지할 노드 이름
- `node_b` (string, 필수): 흡수될 노드 이름
- `merged_summary` (string): Claude가 작성한 통합 요약 (비어있으면 append)

**처리 과정:**
1. 흡수 노드의 alias를 유지 노드로 이전 (중복 alias 자동 제거)
2. 흡수 노드의 이름을 유지 노드의 alias로 추가
3. 흡수 노드의 edge 참조를 유지 노드로 변경
4. 변경 후 중복 edge 합치기 (description 병합)
5. 흡수 노드 soft delete

**출력:** `merged_node`, `edges_migrated`, `edges_deduplicated`, `absorbed_node`

### 3-4. update_node

노드 속성을 직접 수정합니다.

**입력:**
- `name` (string, 필수): 대상 노드 이름
- `summary` (string): 새 요약 (전체 교체, ingest와 달리 append가 아님)
- `category` (string): 새 카테고리
- `add_aliases` (string): 추가 별칭 JSON 배열
- `properties` (string): 추가 속성 JSON 객체 (기존에 merge)

### 3-5. delete_node

노드를 soft delete합니다.

**입력:**
- `name` (string, 필수): 삭제할 노드 이름
- `cascade` (bool, 기본 false): true면 연결 edge도 함께 삭제, false면 edge 있을 시 거부

### 3-6. delete_edge

edge를 soft delete합니다. 두 가지 방식 중 택일:

- `edge_id` (string): ID로 직접 삭제
- `source` + `target` + `relation` (string): 조건으로 삭제 (세 개 모두 필수)

## 4. 성능 특성 (테스트 결과 기반)

| 지표 | 수치 | 비고 |
|------|------|------|
| 100 노드 + 50 edge 조회 | 0.06초 | limit=100 기준 |
| hub 노드 (edge 50개) 조회 | 0.002초 | 단일 노드 |
| 200회 ingestion 후 DB 크기 | 0.46 MB | 1000회 추정 2.3 MB |
| limit=20 응답 토큰 | ~2,326 | 컨텍스트 10% 이내 (안전) |
| limit=100 응답 토큰 | ~11,626 | 10% 초과 (주의) |
| 동시 read-write | 정상 | WAL 모드 |
| 동시 write-write | 순차 처리 | SQLite 자동 재시도 |

## 5. 확장 로드맵

| 기능 | 전환 시점 |
|------|----------|
| Query MCP 분리 | list_nodes 응답이 10K 토큰 초과 |
| 임베딩 유사도 검색 | 노드 500개 이상 + 이름 매칭 실패 빈발 |
| Neo4j 전환 | 3-hop 경로 쿼리 > 1초 or 노드 5,000+ |
| 시각화 (D3.js) | 그래프 구조 확인 필요 시 |
| Maintenance MCP | 수동 merge 빈도 주 3회 이상 |

## 6. 테스트 현황

총 48개 테스트 전체 통과 (2026-06-14 기준)

| 구분 | 테스트 수 | 내용 |
|------|----------|------|
| 기본 E2E | 28 | 도구별 정상/에러 경로 |
| 동시성 | 5 | WAL 모드 멀티스레드 접근 |
| 성능 | 5 | 대량 노드, 토큰 크기, 매칭 정확도 |
| 무결성 | 6 | 특수문자, SQL injection, 외래키, soft delete |
| 복구 | 4 | DB 손상, 삭제 후 재생성, 백업/복원 |

## 7. 알려진 제약

**자기 참조 edge 허용** — source와 target이 같은 edge를 막지 않습니다. 재귀 개념 등에서 유효할 수 있어 의도적으로 허용했습니다.

**오타/한영 변환 매칭 불가** — 퍼지 매칭은 지원하지 않습니다. 임베딩 도입(노드 500+) 시점에서 검토 예정입니다.

**카테고리 고정 목록** — config.toml에 정의된 목록만 허용합니다. 런타임 추가는 config.toml 수정 후 서버 재시작이 필요합니다.

**물리 삭제 미구현** — 현재 모든 삭제는 soft delete이며, 물리 삭제(hard delete)는 Maintenance MCP에서 구현 예정입니다.
