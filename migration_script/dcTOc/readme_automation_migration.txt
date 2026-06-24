# Jira Cloud 자동화 규칙 마이그레이션 도구

Jira Cloud 자동화 규칙을 일괄 변환(Fix)하고 원본으로 복원(Rollback)하는 Python CLI 도구입니다.

## 개요

Jira 인스턴스 마이그레이션 후 자동화 규칙에 발생하는 다음 문제들을 자동으로 감지하고 수정합니다.

| 패턴 | 설명 |
|------|------|
| `PROJECT_KEY` | 구 프로젝트 키(`SGECF`, `SGE_CF`) → 신규 키(`SGCF`) |
| `ISSUE_URL_LINK` | `{{issue.url}}` 플레인 텍스트 → HTML 앵커 태그 |
| `STATUS_CONDITION_TYPE` | 상태 조건의 `compareValue.type=ID` → `NAME` |
| `COPY_SOURCE_FIELD_TYPE` | COPY 액션 `sourceField.type=ID` → `NAME` |
| `TRIGGER_FIELD_REFS` | 트리거 필드 참조 `type="field"` → `"fieldName"` |
| `SELECTED_FIELD_CONDITION` | `selectedField.type=ID` → `NAME` |
| `ISSUE_LINK_BRANCH` | `relatedType="linked"` 단일 항목 → `parent`/`children` |
| `ISSUE_LINK_BRANCH_CHECK` | `linkTypes` 2개 이상인 경우 수동 확인 알림 |
| `MIGRATED_ISSUE_TYPES` | 마이그레이션 이전 이슈 타입 ID → 정식 ID |
| `MIGRATED_CUSTOM_FIELDS` | 마이그레이션 이전 커스텀 필드 ID → 정식 ID |
| `CREATE_ISSUE_LINK` | `issuelinks` 오퍼레이션 → `parent` 오퍼레이션 |
| `BROKEN_CUSTOMFIELD` | 손상된 커스텀 필드 ID 감지 (수동 확인 필요) |

## 요구 사항

- Python 3.10 이상 (외부 라이브러리 없음, 표준 라이브러리만 사용)
- Atlassian API Token

## 설치

```bash
git clone <repository-url>
cd <repository-directory>
```

별도 패키지 설치 불필요합니다.

## 환경 변수 설정

```bash
# 필수
export ATLASSIAN_API_TOKEN=your-api-token

# 선택 (기본값 사용 가능)
export ATLASSIAN_BASE_URL=https://your-instance.atlassian.net   # 기본: sg-cf-usertest.atlassian.net
export ATLASSIAN_EMAIL=your-email@example.com                    # 기본: jsjang@osci.kr
export JIRA_PROJECT_KEY=YOUR_PROJECT                             # 기본: SGCF
export DRY_RUN=false                                             # 기본: true (실제 변경하려면 false)
export MAX_RETRIES=5                                             # 기본: 5
export OUTPUT_DIR=./output                                       # 기본: ./output
```

> **주의:** `DRY_RUN=true`(기본값)이면 변경 내용을 확인만 하고 실제 API PUT은 호출하지 않습니다.

## 실행

```bash
python3 automation_migration.py
```

실행하면 메뉴가 표시됩니다.

```
Automation 작업을 선택하세요:
  1.          규칙 변환 (Fix) — 전체
  1 <rule_id> 규칙 변환 (Fix) — 특정 rule id 만 테스트
  2.          원본 롤백 (Rollback) — output/automation/ 의 원본 JSON 기준
```

### Fix — 전체 규칙 변환

```
>>> 1
```

1. 전체 자동화 규칙 조회
2. 변환 패턴 감지
3. 원본 JSON을 `output/automation/{id}_{name}.json`에 저장
4. 변환 결과를 `output/Jira_Automation.csv`에 저장
5. 변환된 규칙을 Jira API로 PUT

### Fix — 특정 규칙만 테스트

```
>>> 1 12345
```

rule id `12345`에 해당하는 규칙 1개만 처리합니다.

### Rollback — 원본 복원

```
>>> 2
```

`output/automation/` 디렉토리의 원본 JSON 파일을 기반으로 모든 규칙을 복원합니다.

## 출력 파일

```
output/
├── Jira_Automation.csv          # 전체 규칙 목록 및 감지된 패턴
└── automation/
    ├── 12345_규칙이름.json       # Fix 전 원본 (Rollback용)
    └── ...
```

### Jira_Automation.csv 컬럼

| 컬럼 | 설명 |
|------|------|
| `RuleId` | 자동화 규칙 ID |
| `RuleName` | 자동화 규칙 이름 |
| `PROJECT_KEY` ~ `BROKEN_CUSTOMFIELD` | 해당 패턴 감지 여부 (`O` = 감지됨) |
| `Url` | Jira 자동화 설정 페이지 직접 링크 |

## 주의 사항

- **`DRY_RUN=false` 설정 시 실제 Jira 데이터가 변경됩니다.** 실행 전 반드시 확인 프롬프트가 표시됩니다.
- Rollback은 Fix 실행 시 저장한 원본 JSON 파일에만 의존합니다. Fix 실행 전 원본 파일이 없으면 Rollback이 불가능합니다.
- `BROKEN_CUSTOMFIELD` 패턴은 자동 변환이 지원되지 않아 수동 확인이 필요합니다.
- `ISSUE_LINK_BRANCH_CHECK` 패턴(`linkTypes` 2개 이상)도 자동 변환 없이 콘솔에 경고만 출력됩니다.
- 429/5xx 응답 시 지수 백오프로 자동 재시도합니다 (최대 `MAX_RETRIES`회).
