================================================================
  automation_cTOc.py — Jira Cloud → Cloud 자동화 규칙 ID 재매핑 도구
================================================================

■ 개요
------
Jira Cloud 사이트 간 마이그레이션 시, 자동화 규칙에 박힌
커스텀 필드 ID / 이슈 타입 ID / 상태 ID / 링크 타입 ID 등이
사이트마다 달라 오동작하는 문제를 해결하는 도구입니다.

소스/타겟 사이트의 메타데이터를 API로 실시간 조회하여
이름 기준으로 ID 매핑을 자동 생성하고, 타겟 사이트의
자동화 규칙을 일괄 교체합니다.

※ TGT_BASE_URL만 바꾸면 어떤 목적지 사이트에도 동작합니다.
   하드코딩된 ID 없이 매번 API에서 동적으로 매핑을 계산합니다.


■ 요구사항
----------
- Python 3.10 이상 (외부 라이브러리 불필요)
- 소스/타겟 Jira Cloud 사이트의 API 토큰


■ 설정
------
스크립트 상단 또는 환경 변수로 설정합니다.

  변수명            설명                          기본값
  ──────────────────────────────────────────────────────────────
  SRC_BASE_URL      소스 사이트 URL               https://sg-cf-usertest.atlassian.net
  SRC_EMAIL         소스 계정 이메일              jsjang@osci.kr
  SRC_API_TOKEN     소스 API 토큰                 (스크립트 내 기본값)

  TGT_BASE_URL      타겟 사이트 URL               https://sg-cf.atlassian.net
  TGT_EMAIL         타겟 계정 이메일              jsjang@osci.kr
  TGT_API_TOKEN     타겟 API 토큰                 (스크립트 내 기본값)

  TGT_PROJECT_KEY   타겟 프로젝트 키              SGCF
  DRY_RUN           true = 실제 변경 없이 미리보기  false
  MAX_RETRIES       API 재시도 횟수               5
  OUTPUT_DIR        결과 파일 저장 경로           ./output

환경 변수 사용 예:
  export SRC_API_TOKEN=your_token
  export TGT_API_TOKEN=your_token
  export TGT_BASE_URL=https://your-site.atlassian.net


■ 실행 방법
-----------
  python3 automation_cTOc.py

실행 후 메뉴에서 작업을 선택합니다:

  1            ID 재매핑 (Fix) — 타겟 사이트 전체 자동화 규칙 교체
  1 <rule_id>  ID 재매핑 (Fix) — 특정 룰 ID만 테스트
  2            원본 롤백 (Rollback) — 교체 전 원본으로 되돌리기
  3            소스 ENABLED 룰 → 타겟 활성화 동기화
  4            타겟 전체 룰 비활성화


■ 각 메뉴 설명
--------------
[1] ID 재매핑 (Fix)
  - 소스/타겟 사이트에서 필드·이슈타입·상태·링크타입 목록을 조회
  - 이름이 같은 항목끼리 소스ID → 타겟ID 매핑 자동 생성
  - 타겟 사이트의 자동화 규칙에서 소스ID를 찾아 타겟ID로 교체
  - 처리 전 원본 JSON을 output/automation_cTOc/ 에 백업
  - 매핑 결과를 output/field_id_mapping.csv 에 저장

[2] 원본 롤백 (Rollback)
  - output/automation_cTOc/ 에 저장된 원본 JSON으로 복원
  - 1단계: 원본 그대로 PUT 시도
  - 2단계: 실패 시 컴포넌트 ID 제거 후 재시도

[3] 소스 ENABLED 룰 → 타겟 활성화 동기화
  - 소스 사이트에서 ENABLED 상태인 룰 목록을 조회
  - 같은 이름의 룰을 타겟 사이트에서 찾아 ENABLED로 변경
  - 타겟에 없는 룰은 목록 출력

[4] 타겟 전체 룰 비활성화
  - 타겟 사이트의 모든 ENABLED 룰을 DISABLED로 변경


■ 자동 매핑 실패 시 (이름이 다른 경우)
---------------------------------------
스크립트 상단의 오버라이드 맵에 직접 지정합니다:

  FIELD_ID_OVERRIDE_MAP = {
      "customfield_10119": "customfield_10234",
  }

  ISSUE_TYPE_ID_OVERRIDE_MAP = {
      "10009": "10020",
  }

  STATUS_ID_OVERRIDE_MAP = {
      "10060": "10070",
  }

  LINK_TYPE_ID_OVERRIDE_MAP = {
      "10010": "10008",
  }


■ 출력 파일
-----------
  output/
  ├── automation_cTOc/
  │   └── {rule_id}_{rule_name}.json   교체 전 원본 백업 (롤백용)
  ├── field_id_mapping.csv             커스텀필드/이슈타입/상태/링크타입 매핑 결과
  └── automation_cTOc_result.csv       룰별 교체 여부 결과


■ 주의사항
----------
- DRY_RUN=false 상태에서 실행하면 타겟 사이트 자동화 규칙이 실제로 변경됩니다.
- 처음 실행 시 DRY_RUN=true(기본값)로 먼저 결과를 확인하세요.
- Fix 실행 전 원본이 output/automation_cTOc/ 에 자동 백업됩니다.
  문제 발생 시 메뉴 2번으로 롤백할 수 있습니다.
- 이슈 타입 이름이 소스/타겟 간에 다를 경우 자동 매핑이 되지 않으므로
  ISSUE_TYPE_ID_OVERRIDE_MAP에 직접 지정이 필요합니다.


■ 문제가 될 수 있는 환경
--------------------------

[1] 동일한 이름의 커스텀 필드가 두 개 이상 존재하는 경우  ★ 가장 주의 필요
  - 이름 → ID 매핑 시 dict를 사용하므로 같은 이름이 있으면 마지막 항목이
    앞 항목을 덮어씁니다. 나머지 필드는 경고 없이 무시됩니다.
  - 증상: 특정 필드가 엉뚱한 타겟 필드로 매핑되거나 매핑 누락
  - 대응: FIELD_ID_OVERRIDE_MAP에 해당 필드를 직접 지정

[2] 소스/타겟 간 이름이 달라 매핑이 안 되는 경우
  - 이름이 다르면 매핑이 생성되지 않아 소스 ID가 그대로 남습니다.
  - 증상: API 400 오류 ("Please select a valid work type." 등)
  - 대응: *_OVERRIDE_MAP에 소스ID → 타겟ID 직접 지정
  - 참고: 이슈 타입은 사이트마다 이름이 달라 자주 발생합니다.
          Fix 실행 후 field_id_mapping.csv에서 매핑 결과를 확인하세요.

[3] 타겟 사이트에 해당 필드/이슈타입 자체가 없는 경우
  - 매핑이 생성되지 않아 소스 ID가 그대로 남습니다.
  - 증상: API 400 오류
  - 대응: 타겟 사이트에 필드/이슈타입 생성 후 재실행하거나,
          해당 룰의 해당 액션을 수동 편집

[4] status / 이슈타입 ID가 다른 값의 숫자와 우연히 일치하는 경우
  - 문자열 치환 단계에서 의도치 않은 숫자값을 교체할 수 있습니다.
  - 구조 탐색(replace_ids_in_node)이 먼저 처리되어 실제 발생 빈도는 낮습니다.
  - 문자열 치환 단계는 구조 탐색으로 교체되지 못한 나머지만 처리합니다.

[5] 소스/타겟 필드 ID가 우연히 동일한 경우
  - ID가 같으면 매핑 불필요로 판단하여 건너뜁니다. (정상 동작)
  - 단, 같은 ID지만 실제로는 다른 필드인 경우(극히 드뭄) 오매핑이 발생할 수 있습니다.

  상황                          증상              대응
  ──────────────────────────────────────────────────────────────────────
  같은 이름 필드 중복            무음 오매핑        OVERRIDE_MAP 수동 지정
  이름 불일치로 매핑 실패        API 400 오류       OVERRIDE_MAP 수동 지정
  타겟에 필드 자체가 없음        API 400 오류       필드 생성 후 재실행
  숫자 ID 패턴 충돌              드물게 오치환      구조 탐색 우선이라 빈도 낮음
  ID가 동일한 경우               건너뜀 (정상)      조치 불필요
================================================================
