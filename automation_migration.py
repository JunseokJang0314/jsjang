#!/usr/bin/env python3
"""
Jira Cloud 자동화 규칙 마이그레이션 도구 (Python 구현)

Java AutomationService와 동일한 로직:
- Fix:      전체 자동화 규칙 조회 → 패턴 감지 → 원본 JSON 저장 → 변환 후 PUT
- Rollback: output/automation/ 의 원본 JSON 기반 일괄 복원

사용법:
    export ATLASSIAN_API_TOKEN=your-token
    python3 automation_migration.py

환경 변수 (ATLASSIAN_API_TOKEN 만 필수, 나머지는 기본값 사용 가능):
    ATLASSIAN_BASE_URL      기본값: https://sg-cf-migtest1.atlassian.net
    ATLASSIAN_EMAIL         기본값: jsjang@osci.kr
    ATLASSIAN_API_TOKEN     필수
    JIRA_PROJECT_KEY        기본값: SGCF
    DRY_RUN                 기본값: true (false 로 설정해야 실제 변경)
    MAX_RETRIES             기본값: 5
    OUTPUT_DIR              기본값: ./output
"""

import base64
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional


# ── 설정 ──────────────────────────────────────────────────────────────────────

BASE_URL       = os.getenv("ATLASSIAN_BASE_URL", "https://sg-cf-usertest.atlassian.net")
EMAIL          = os.getenv("ATLASSIAN_EMAIL", "jsjang@osci.kr")
API_TOKEN      = os.getenv("ATLASSIAN_API_TOKEN", "")
PROJECT_KEY    = os.getenv("JIRA_PROJECT_KEY", "SGCF")
DRY_RUN        = os.getenv("DRY_RUN", "ture").lower() != "false"
MAX_RETRIES    = int(os.getenv("MAX_RETRIES", "5"))
OUTPUT_DIR     = Path(os.getenv("OUTPUT_DIR", "./output"))
PAGE_SIZE      = 100
AUTOMATION_PATH = "/gateway/api/automation/internal-api/jira/{cloud_id}/pro/rest/GLOBAL"

# ── 패턴 상수 ────────────────────────────────────────────────────────────────

FIX_PROJECT_KEY            = "PROJECT_KEY"
FIX_ISSUE_URL_LINK         = "ISSUE_URL_LINK"
FIX_STATUS_CONDITION_TYPE  = "STATUS_CONDITION_TYPE"
FIX_COPY_SOURCE_FIELD_TYPE = "COPY_SOURCE_FIELD_TYPE"
FIX_TRIGGER_FIELD_REFS     = "TRIGGER_FIELD_REFS"
FIX_SELECTED_FIELD_COND    = "SELECTED_FIELD_CONDITION"
FIX_ISSUE_LINK_BRANCH      = "ISSUE_LINK_BRANCH"
FIX_MIGRATED_ISSUE_TYPES   = "MIGRATED_ISSUE_TYPES"
FIX_MIGRATED_CUSTOM_FIELDS = "MIGRATED_CUSTOM_FIELDS"
FIX_CREATE_ISSUE_LINK           = "CREATE_ISSUE_LINK"
FIX_BROKEN_CUSTOMFIELD          = "BROKEN_CUSTOMFIELD"
CHECK_ISSUE_LINK_BRANCH_MULTI   = "ISSUE_LINK_BRANCH_CHECK"

FIX_TYPE_COLUMNS = [
    FIX_PROJECT_KEY,
    FIX_ISSUE_URL_LINK,
    FIX_STATUS_CONDITION_TYPE,
    FIX_COPY_SOURCE_FIELD_TYPE,
    FIX_TRIGGER_FIELD_REFS,
    FIX_SELECTED_FIELD_COND,
    FIX_ISSUE_LINK_BRANCH,
    CHECK_ISSUE_LINK_BRANCH_MULTI,
    FIX_MIGRATED_ISSUE_TYPES,
    FIX_MIGRATED_CUSTOM_FIELDS,
    FIX_CREATE_ISSUE_LINK,
    FIX_BROKEN_CUSTOMFIELD,
]

# 마이그레이션 이전 이슈 타입 이름 → 정식 이름 쌍
MIGRATED_ISSUE_TYPE_NAMES = [
    ("하위 작업 (Migrated)", "하위 작업"),
    ("Epic (migrated)",      "에픽"),
    ("Epic (Migrated)",      "에픽"),
]

# 마이그레이션 이전 커스텀 필드 이름 → 정식 이름 쌍
MIGRATED_CUSTOM_FIELD_NAMES = [
    ("버전 (Migrated)", "버전"),
]

BROKEN_CUSTOMFIELD_ID = "9223372036854775807"
BLOCKS_LINK_TYPE_NAME = "Blocks"


# ── HTTP 클라이언트 ───────────────────────────────────────────────────────────

def _auth_header() -> str:
    token = base64.b64encode(f"{EMAIL}:{API_TOKEN}".encode()).decode()
    return f"Basic {token}"


def _retryable(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _request(method: str, url: str, body: Any = None) -> Any:
    """재시도 포함 HTTP 요청 (429/5xx → 지수 백오프). 응답 JSON을 반환한다."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": _auth_header(),
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    for attempt in range(1, MAX_RETRIES + 2):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            status = e.code
            body_text = e.read().decode("utf-8", errors="replace")[:500]
            if _retryable(status) and attempt <= MAX_RETRIES:
                wait = 2 * attempt
                print(f"  [RETRY {attempt}/{MAX_RETRIES}] {status} — {url} — {wait}s 대기")
                time.sleep(wait)
                continue
            raise RuntimeError(f"API 오류 {status}: {url}\n{body_text}")
    raise RuntimeError(f"최대 재시도 초과: {url}")


def get_json(url: str) -> Any:
    return _request("GET", url)


def post_json(url: str, body: Any) -> Any:
    return _request("POST", url, body)


def put_json(url: str, body: Any) -> None:
    _request("PUT", url, body)


# ── Jira API 조회 ─────────────────────────────────────────────────────────────

_cloud_id_cache: Optional[str] = None


def fetch_cloud_id() -> str:
    global _cloud_id_cache
    if _cloud_id_cache:
        return _cloud_id_cache
    info = get_json(f"{BASE_URL}/_edge/tenant_info")
    _cloud_id_cache = info["cloudId"]
    print(f"cloudId: {_cloud_id_cache}")
    return _cloud_id_cache


def automation_base() -> str:
    return BASE_URL + AUTOMATION_PATH.format(cloud_id=fetch_cloud_id())


def fetch_all_rules() -> list[dict]:
    """전체 자동화 규칙을 페이지네이션으로 조회한다."""
    all_rules: list[dict] = []
    offset = 0
    base = automation_base()
    while True:
        resp = post_json(f"{base}/rules", {"limit": PAGE_SIZE, "offset": offset})
        values = resp.get("values", [])
        if not values:
            break
        all_rules.extend(values)
        total = resp.get("total", 0)
        offset += len(values)
        if offset >= total:
            break
    return all_rules


def fetch_fields() -> list[dict]:
    """전체 Jira 필드 목록 조회."""
    return get_json(f"{BASE_URL}/rest/api/3/field")


def fetch_statuses() -> dict[str, str]:
    """status ID → 상태명 매핑."""
    statuses = get_json(f"{BASE_URL}/rest/api/3/status")
    result = {str(s["id"]): s["name"] for s in statuses}
    print(f"Status 매핑 조회 완료: {len(result)}개")
    return result


def fetch_issue_types() -> list[dict]:
    return get_json(f"{BASE_URL}/rest/api/3/issuetype")


def fetch_issue_link_types() -> list[dict]:
    resp = get_json(f"{BASE_URL}/rest/api/3/issueLinkType")
    return resp.get("issueLinkTypes", [])


def resolve_migrated_issue_type_ids() -> dict[str, str]:
    """마이그레이션 이전 이슈 타입 ID → 정식 ID 매핑."""
    issue_types = fetch_issue_types()
    name_to_id = {it["name"]: str(it["id"]) for it in issue_types if "name" in it}
    result: dict[str, str] = {}
    for from_name, to_name in MIGRATED_ISSUE_TYPE_NAMES:
        from_id = name_to_id.get(from_name)
        to_id   = name_to_id.get(to_name)
        if from_id and to_id:
            result[from_id] = to_id
            print(f"이슈 타입 ID 매핑: {from_name} ({from_id}) → {to_name} ({to_id})")
        else:
            print(f"[WARN] 이슈 타입 조회 실패: fromName={from_name}({from_id}), toName={to_name}({to_id})")
    print(f"MIGRATED_ISSUE_TYPES 매핑 조회 완료: {len(result)}개")
    return result


def resolve_migrated_custom_field_ids(fields: list[dict]) -> dict[str, str]:
    """마이그레이션 이전 커스텀 필드 ID → 정식 ID 매핑."""
    name_to_id = {
        f["name"]: f["id"]
        for f in fields
        if f.get("id", "").startswith("customfield_") and f.get("name")
    }
    result: dict[str, str] = {}
    for from_name, to_name in MIGRATED_CUSTOM_FIELD_NAMES:
        from_id = name_to_id.get(from_name)
        to_id   = name_to_id.get(to_name)
        if from_id and to_id:
            result[from_id] = to_id
            print(f"커스텀 필드 ID 매핑: {from_name} ({from_id}) → {to_name} ({to_id})")
        else:
            print(f"[WARN] 커스텀 필드 조회 실패: fromName={from_name}({from_id}), toName={to_name}({to_id})")
    print(f"MIGRATED_CUSTOM_FIELDS 매핑 조회 완료: {len(result)}개")
    return result


def resolve_migrated_custom_field_schemas(
    fields: list[dict], migrated_cf_ids: dict[str, str]
) -> dict[str, str]:
    """마이그레이션 이전 커스텀 필드 ID → 정식 필드의 schema.custom 매핑."""
    new_id_to_schema = {
        f["id"]: f["schema"]["custom"]
        for f in fields
        if f.get("id") and isinstance(f.get("schema"), dict) and f["schema"].get("custom")
    }
    result: dict[str, str] = {}
    for old_id, new_id in migrated_cf_ids.items():
        schema = new_id_to_schema.get(new_id)
        if schema:
            result[old_id] = schema
            print(f"커스텀 필드 schema 매핑: {old_id} → {schema}")
        else:
            print(f"[WARN] 커스텀 필드 schema 조회 실패: newId={new_id}")
    return result


def resolve_blocks_link_type_id() -> Optional[str]:
    """'Blocks' 이슈 링크 타입 ID 동적 조회."""
    for lt in fetch_issue_link_types():
        if lt.get("name") == BLOCKS_LINK_TYPE_NAME:
            link_id = str(lt["id"])
            print(f"Blocks 이슈 링크 타입 ID 조회 완료: {link_id}")
            return link_id
    print(f"[WARN] '{BLOCKS_LINK_TYPE_NAME}' 이슈 링크 타입을 찾을 수 없음")
    return None


def build_field_id_to_name(fields: list[dict]) -> dict[str, str]:
    return {f["id"]: f["name"] for f in fields if f.get("id") and f.get("name")}


# ── 패턴 감지 ─────────────────────────────────────────────────────────────────

def detect_fix_types(
    rule_json: str,
    migrated_issue_type_ids: dict[str, str],
    migrated_cf_ids: dict[str, str],
    blocks_link_type_id: Optional[str],
) -> list[str]:
    fix_types: list[str] = []

    if "SGECF" in rule_json or "SGE_CF" in rule_json:
        fix_types.append(FIX_PROJECT_KEY)

    if "{{issue.url}}" in rule_json:
        fix_types.append(FIX_ISSUE_URL_LINK)

    if '"value":"status"' in rule_json:
        fix_types.append(FIX_STATUS_CONDITION_TYPE)

    if '"type":"COPY"' in rule_json and '"sourceField":{"type":"ID","value":"customfield_' in rule_json:
        fix_types.append(FIX_COPY_SOURCE_FIELD_TYPE)

    if '"type":"field"' in rule_json and '"customfield_' in rule_json:
        fix_types.append(FIX_TRIGGER_FIELD_REFS)

    if '"selectedField":{"type":"ID","value":"customfield_' in rule_json:
        fix_types.append(FIX_SELECTED_FIELD_COND)

    if '"relatedType":"linked"' in rule_json:
        import re as _re
        if _re.search(r'"linkTypes":\["[^"]+","', rule_json):
            fix_types.append(CHECK_ISSUE_LINK_BRANCH_MULTI)
        else:
            fix_types.append(FIX_ISSUE_LINK_BRANCH)

    if migrated_issue_type_ids and any(k in rule_json for k in migrated_issue_type_ids):
        fix_types.append(FIX_MIGRATED_ISSUE_TYPES)

    if migrated_cf_ids and any(k in rule_json for k in migrated_cf_ids):
        fix_types.append(FIX_MIGRATED_CUSTOM_FIELDS)

    if '"value":"issuelinks"' in rule_json and '"linkType":' in rule_json:
        fix_types.append(FIX_CREATE_ISSUE_LINK)

    if BROKEN_CUSTOMFIELD_ID in rule_json:
        fix_types.append(FIX_BROKEN_CUSTOMFIELD)

    return fix_types


# ── 변환 함수 (재귀 탐색) ──────────────────────────────────────────────────────

def fix_status_conditions(node: Any, status_id_to_name: dict[str, str]) -> None:
    """selectedField.value="status" + compareValue.type=ID → type=NAME(상태명).
    multiValue=true 인 경우 JSON 배열 내 ID 목록도 함께 변환한다.
    """
    if isinstance(node, dict):
        sf = node.get("selectedField")
        cv = node.get("compareValue")
        if isinstance(sf, dict) and isinstance(cv, dict):
            if sf.get("value") == "status" and cv.get("type") == "ID":
                node["selectedFieldType"] = "status"
                if cv.get("multiValue") is True:
                    # 배열 형태: "[\"10060\",\"10059\",...]"
                    try:
                        ids: list[str] = json.loads(cv["value"])
                        names = []
                        for sid in ids:
                            name = status_id_to_name.get(str(sid))
                            if name:
                                print(f"  Status 조건 변환 (multi): ID {sid} → NAME {name}")
                                names.append(name)
                            else:
                                print(f"  [WARN] Status ID {sid}에 해당하는 상태명 없음")
                                names.append(sid)
                        cv["type"]  = "NAME"
                        cv["value"] = json.dumps(names)
                    except (json.JSONDecodeError, TypeError):
                        pass
                else:
                    # 단일 값
                    status_id   = str(cv.get("value", ""))
                    status_name = status_id_to_name.get(status_id)
                    if status_name:
                        cv["type"]  = "NAME"
                        cv["value"] = status_name
                        print(f"  Status 조건 변환: ID {status_id} → NAME {status_name}")
                    else:
                        print(f"  [WARN] Status ID {status_id}에 해당하는 상태명 없음")
        for v in node.values():
            fix_status_conditions(v, status_id_to_name)
    elif isinstance(node, list):
        for item in node:
            fix_status_conditions(item, status_id_to_name)


def fix_copy_source_field_type(node: Any, field_id_to_name: dict[str, str]) -> None:
    """COPY 액션의 sourceField: type=ID+customfield_* → type=NAME+필드명."""
    if isinstance(node, dict):
        if node.get("type") == "COPY":
            outer_field_type = node.get("fieldType")
            value_obj = node.get("value")
            if isinstance(value_obj, dict):
                sf = value_obj.get("sourceField")
                if isinstance(sf, dict):
                    sf_type  = sf.get("type")
                    sf_value = sf.get("value", "")
                    if sf_type == "ID" and str(sf_value).startswith("customfield_"):
                        field_name = field_id_to_name.get(sf_value)
                        if field_name:
                            sf["type"]  = "NAME"
                            sf["value"] = field_name
                            print(f"  COPY sourceField 변환: {sf_value} → NAME:{field_name}")
                        else:
                            print(f"  [WARN] COPY sourceField 필드명 조회 실패: {sf_value}")
                    if sf.get("fieldType") is None and outer_field_type is not None:
                        sf["fieldType"] = outer_field_type
        for v in node.values():
            fix_copy_source_field_type(v, field_id_to_name)
    elif isinstance(node, list):
        for item in node:
            fix_copy_source_field_type(item, field_id_to_name)


def fix_trigger_field_refs(node: Any, field_id_to_name: dict[str, str]) -> None:
    """type="field"+value=customfield_* → type="fieldName"+value=필드명."""
    if isinstance(node, dict):
        if node.get("type") == "field":
            value = node.get("value", "")
            if str(value).startswith("customfield_"):
                field_name = field_id_to_name.get(value)
                if field_name:
                    node["type"]  = "fieldName"
                    node["value"] = field_name
                    print(f"  TRIGGER 필드 참조 변환: {value} → fieldName:{field_name}")
                else:
                    print(f"  [WARN] TRIGGER 필드 참조 이름 조회 실패: {value}")
        for v in node.values():
            fix_trigger_field_refs(v, field_id_to_name)
    elif isinstance(node, list):
        for item in node:
            fix_trigger_field_refs(item, field_id_to_name)


def fix_selected_field_condition(node: Any, field_id_to_name: dict[str, str]) -> None:
    """selectedField.type=ID+value=customfield_* → type=NAME+value=필드명."""
    if isinstance(node, dict):
        sf = node.get("selectedField")
        if isinstance(sf, dict):
            sf_type  = sf.get("type")
            sf_value = sf.get("value", "")
            if sf_type == "ID" and str(sf_value).startswith("customfield_"):
                field_name = field_id_to_name.get(sf_value)
                if field_name:
                    sf["type"]  = "NAME"
                    sf["value"] = field_name
                    print(f"  SELECTED_FIELD 조건 변환: {sf_value} → NAME:{field_name}")
                else:
                    print(f"  [WARN] SELECTED_FIELD 이름 조회 실패: {sf_value}")
        for v in node.values():
            fix_selected_field_condition(v, field_id_to_name)
    elif isinstance(node, list):
        for item in node:
            fix_selected_field_condition(item, field_id_to_name)


def fix_issue_link_branch(node: Any, related_type: str) -> None:
    """relatedType="linked"+linkTypes 단일 항목 → relatedType으로 변환."""
    if isinstance(node, dict):
        if node.get("type") == "jira.issue.related":
            value_obj = node.get("value")
            if isinstance(value_obj, dict):
                link_types = value_obj.get("linkTypes", [])
                if value_obj.get("relatedType") == "linked" and len(link_types) == 1:
                    value_obj["relatedType"] = related_type
                    print(f"  ISSUE_LINK_BRANCH 변환: linked{link_types} → {related_type}")
        for v in node.values():
            fix_issue_link_branch(v, related_type)
    elif isinstance(node, list):
        for item in node:
            fix_issue_link_branch(item, related_type)


def fix_migrated_issue_types(node: Any, migrated_ids: dict[str, str]) -> None:
    """compareValue 내 마이그레이션 이전 이슈 타입 ID → 정식 ID 교체."""
    if isinstance(node, dict):
        cv = node.get("compareValue")
        if isinstance(cv, dict) and isinstance(cv.get("value"), str):
            if cv.get("multiValue") is True:
                # 배열 형태: "[\"10043\",\"10041\"]"
                try:
                    ids: list[str] = json.loads(cv["value"])
                    changed = False
                    for i, issue_id in enumerate(ids):
                        replacement = migrated_ids.get(issue_id)
                        if replacement:
                            print(f"  이슈 타입 ID 교체 (multi): {issue_id} → {replacement}")
                            ids[i] = replacement
                            changed = True
                    if changed:
                        cv["value"] = json.dumps(ids)
                except (json.JSONDecodeError, TypeError):
                    pass
            else:
                # 단일 값: "10043"
                replacement = migrated_ids.get(cv["value"])
                if replacement:
                    print(f"  이슈 타입 ID 교체 (single): {cv['value']} → {replacement}")
                    cv["value"] = replacement
        for v in node.values():
            fix_migrated_issue_types(v, migrated_ids)
    elif isinstance(node, list):
        for item in node:
            fix_migrated_issue_types(item, migrated_ids)


def fix_migrated_custom_field_ops(
    node: Any,
    id_map: dict[str, str],
    schema_map: dict[str, str],
) -> None:
    """operation의 field.value(구 ID)와 fieldType을 함께 교체."""
    if isinstance(node, dict):
        field_obj = node.get("field")
        if isinstance(field_obj, dict):
            field_value = field_obj.get("value")
            new_id = id_map.get(field_value)
            if new_id:
                field_obj["value"] = new_id
                new_schema = schema_map.get(field_value)
                if new_schema and "fieldType" in node:
                    old_schema = node.get("fieldType")
                    node["fieldType"] = new_schema
                    print(f"  MIGRATED_CUSTOM_FIELDS: field.value {field_value} → {new_id}, "
                          f"fieldType {old_schema} → {new_schema}")
        for v in node.values():
            fix_migrated_custom_field_ops(v, id_map, schema_map)
    elif isinstance(node, list):
        for item in node:
            fix_migrated_custom_field_ops(item, id_map, schema_map)


def fix_create_issue_link(node: Any, blocks_link_type_id: Optional[str]) -> None:
    """jira.issue.create 액션의 issuelinks operation → parent operation으로 변환."""
    if isinstance(node, dict):
        if node.get("type") == "jira.issue.create":
            value_obj = node.get("value")
            if isinstance(value_obj, dict):
                ops = value_obj.get("operations")
                if isinstance(ops, list):
                    new_ops = []
                    for op in ops:
                        if not isinstance(op, dict):
                            new_ops.append(op)
                            continue
                        field  = op.get("field")
                        op_val = op.get("value")
                        if (isinstance(field, dict) and field.get("value") == "issuelinks"
                                and isinstance(op_val, dict)):
                            issue_ref = "trigger"
                            issue_obj = op_val.get("issue")
                            if isinstance(issue_obj, dict):
                                issue_ref = str(issue_obj.get("value", "trigger"))
                            parent_op = {
                                "field":     {"type": "ID", "value": "parent"},
                                "fieldType": "parent",
                                "type":      "SET",
                                "value":     {"type": "COPY", "value": issue_ref},
                            }
                            new_ops.append(parent_op)
                            print(f"  CREATE_ISSUE_LINK 변환: issuelinks(linkType={op_val.get('linkType')}) "
                                  f"→ parent({issue_ref})")
                        else:
                            new_ops.append(op)
                    value_obj["operations"] = new_ops
        for v in node.values():
            fix_create_issue_link(v, blocks_link_type_id)
    elif isinstance(node, list):
        for item in node:
            fix_create_issue_link(item, blocks_link_type_id)


# ── 규칙 변환 진입점 ──────────────────────────────────────────────────────────

def transform_rule(
    rule: dict,
    fix_types: list[str],
    status_id_to_name: dict[str, str],
    field_id_to_name: dict[str, str],
    migrated_issue_type_ids: dict[str, str],
    migrated_cf_ids: dict[str, str],
    migrated_cf_schemas: dict[str, str],
    blocks_link_type_id: Optional[str],
) -> dict:
    """
    fix_types 목록에 따라 rule dict를 변환하여 반환한다.
    원본 dict를 직접 수정하지 않도록 deep copy 후 처리한다.
    """
    import copy
    result = copy.deepcopy(rule)
    rule_json_str = json.dumps(result, ensure_ascii=False, separators=(',', ':'))

    for fix_type in fix_types:
        if fix_type == FIX_PROJECT_KEY:
            rule_json_str = rule_json_str.replace("SGECF", "SGCF").replace("SGE_CF", "SGCF")
            result = json.loads(rule_json_str)

        elif fix_type == FIX_ISSUE_URL_LINK:
            # JSON 문자열 컨텍스트에서 이스케이프된 형태로 치환
            rule_json_str = rule_json_str.replace(
                "{{issue.url}}",
                '<a href=\\"{{issue.url}}\\">{{issue.url}}</a>'
            )
            result = json.loads(rule_json_str)

        elif fix_type == FIX_STATUS_CONDITION_TYPE:
            fix_status_conditions(result, status_id_to_name)
            rule_json_str = json.dumps(result, ensure_ascii=False, separators=(',', ':'))

        elif fix_type == FIX_COPY_SOURCE_FIELD_TYPE:
            fix_copy_source_field_type(result, field_id_to_name)
            rule_json_str = json.dumps(result, ensure_ascii=False, separators=(',', ':'))

        elif fix_type == FIX_TRIGGER_FIELD_REFS:
            fix_trigger_field_refs(result, field_id_to_name)
            rule_json_str = json.dumps(result, ensure_ascii=False, separators=(',', ':'))

        elif fix_type == FIX_SELECTED_FIELD_COND:
            fix_selected_field_condition(result, field_id_to_name)
            rule_json_str = json.dumps(result, ensure_ascii=False, separators=(',', ':'))

        elif fix_type == FIX_ISSUE_LINK_BRANCH:
            related_type = "children" if "issuetype = 이야기" in rule_json_str else "parent"
            fix_issue_link_branch(result, related_type)
            rule_json_str = json.dumps(result, ensure_ascii=False, separators=(',', ':'))

        elif fix_type == CHECK_ISSUE_LINK_BRANCH_MULTI:
            import re as _re
            matches = _re.findall(r'"linkTypes":\[([^\]]+)\]', rule_json_str)
            for m in matches:
                print(f"  [CHECK][ISSUE_LINK_BRANCH] linkTypes 2개 이상 — 확인 필요: [{m}]")

        elif fix_type == FIX_MIGRATED_ISSUE_TYPES:
            fix_migrated_issue_types(result, migrated_issue_type_ids)
            rule_json_str = json.dumps(result, ensure_ascii=False, separators=(',', ':'))

        elif fix_type == FIX_MIGRATED_CUSTOM_FIELDS:
            # 1. 재귀 탐색: operation의 field.value + fieldType 함께 교체
            fix_migrated_custom_field_ops(result, migrated_cf_ids, migrated_cf_schemas)
            rule_json_str = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
            # 2. 문자열 교체: 나머지 컨텍스트의 구 ID 교체
            for old_id, new_id in migrated_cf_ids.items():
                rule_json_str = rule_json_str.replace(old_id, new_id)
            result = json.loads(rule_json_str)

        elif fix_type == FIX_CREATE_ISSUE_LINK:
            fix_create_issue_link(result, blocks_link_type_id)
            rule_json_str = json.dumps(result, ensure_ascii=False, separators=(',', ':'))

        elif fix_type == FIX_BROKEN_CUSTOMFIELD:
            print(f"  [WARN][BROKEN_CUSTOMFIELD] 변환 미지원 — 수동 확인 필요: {BROKEN_CUSTOMFIELD_ID}")

    return result


# ── 저장 ──────────────────────────────────────────────────────────────────────

def save_original_jsons(target_rules: list[dict]) -> None:
    """변환 대상 규칙의 원본 JSON을 output/automation/{id}_{name}.json 으로 저장."""
    automation_dir = OUTPUT_DIR / "automation"
    automation_dir.mkdir(parents=True, exist_ok=True)

    for rule in target_rules:
        rule_id   = rule["id"]
        rule_name = re.sub(r'[\\/:*?"<>|]', "_", rule.get("name", ""))
        file_path = automation_dir / f"{rule_id}_{rule_name}.json"
        file_path.write_text(json.dumps(rule, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"원본 JSON 저장 완료: {len(target_rules)}개 → {automation_dir.resolve()}")


def _build_rule_url(rule: dict) -> str:
    tags      = rule.get("tags") or []
    rule_uuid = tags[0].get("ruleIdUuid", "") if tags else ""
    trigger   = rule.get("trigger") or {}
    trig_id   = trigger.get("id", "")
    return (f"{BASE_URL}/jira/software/c/projects/{PROJECT_KEY}"
            f"/settings/automate#/rule/{rule_uuid}/{trig_id}")


def save_csv(
    all_rules: list[dict],
    target_rule_ids: set,
    migrated_issue_type_ids: dict[str, str],
    migrated_cf_ids: dict[str, str],
    blocks_link_type_id: Optional[str],
) -> None:
    """Jira_Automation.csv 저장."""
    output_path = OUTPUT_DIR / "Jira_Automation.csv"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["RuleId", "RuleName"] + FIX_TYPE_COLUMNS + ["Url"])

        for rule in all_rules:
            rule_id   = str(rule["id"])
            rule_name = rule.get("name", "")
            url       = _build_rule_url(rule)

            if rule["id"] in target_rule_ids:
                detected = detect_fix_types(
                    json.dumps(rule, ensure_ascii=False, separators=(',', ':')),
                    migrated_issue_type_ids, migrated_cf_ids, blocks_link_type_id,
                )
            else:
                detected = []

            row = [rule_id, rule_name] + ["O" if col in detected else "" for col in FIX_TYPE_COLUMNS] + [url]
            writer.writerow(row)

    print(f"Saved {len(all_rules)} rules to {output_path.resolve()}")


# ── Rollback 전처리 ───────────────────────────────────────────────────────────

# Jira는 PUT 후 컴포넌트 ID를 재생성한다.
# 저장된 원본 JSON의 id/parentId/conditionParentId/checksum 이 현재 DB와 불일치하므로 제거한다.
# 계층 구조는 children/conditions 중첩 배열로 보존되므로 Jira가 새 ID를 할당할 수 있다.
_COMPONENT_ID_FIELDS = {"id", "parentId", "conditionParentId", "checksum"}


def strip_component_refs(rule: dict) -> dict:
    """
    Rollback PUT 전처리: 중첩 컴포넌트의 ID 참조 필드를 모두 제거한다.

    제거 대상: id, parentId, conditionParentId, checksum
    유지 대상: 최상위 rule id (업데이트 대상 식별에 필요)
    """
    import copy

    def _strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: _strip(v) for k, v in node.items()
                    if k not in _COMPONENT_ID_FIELDS}
        if isinstance(node, list):
            return [_strip(item) for item in node]
        return node

    top_level_id = rule.get("id")
    result = _strip(copy.deepcopy(rule))
    if top_level_id is not None:
        result["id"] = top_level_id
    return result


# ── Fix / Rollback ────────────────────────────────────────────────────────────

def fix_automation_rules(filter_rule_id: Optional[int] = None) -> None:
    """
    1. cloudId 조회
    2. 전체 자동화 규칙 목록 조회
    3. 필드/상태/이슈 타입 등 매핑 조회
    4. 패턴 감지 → 원본 JSON 저장 → CSV 저장
    5. 변환 후 PUT

    filter_rule_id: 지정 시 해당 rule id 1개만 처리 (테스트용)
    """
    base = automation_base()

    all_rules = fetch_all_rules()
    print(f"전체 규칙 조회 완료: {len(all_rules)}개")

    if filter_rule_id is not None:
        all_rules = [r for r in all_rules if r["id"] == filter_rule_id]
        if not all_rules:
            print(f"[ERROR] rule id={filter_rule_id} 를 찾을 수 없습니다.")
            return
        print(f"테스트 모드: rule id={filter_rule_id} 만 처리합니다.")

    fields               = fetch_fields()
    field_id_to_name     = build_field_id_to_name(fields)
    status_id_to_name    = fetch_statuses()
    migrated_it_ids      = resolve_migrated_issue_type_ids()
    migrated_cf_ids      = resolve_migrated_custom_field_ids(fields)
    migrated_cf_schemas  = resolve_migrated_custom_field_schemas(fields, migrated_cf_ids)
    blocks_link_type_id  = resolve_blocks_link_type_id()

    # 패턴 감지
    targets: list[tuple[dict, list[str]]] = []
    for rule in all_rules:
        rule_json = json.dumps(rule, ensure_ascii=False, separators=(',', ':'))
        fix_types = detect_fix_types(rule_json, migrated_it_ids, migrated_cf_ids, blocks_link_type_id)
        if fix_types:
            targets.append((rule, fix_types))

    print(f"변환 대상 규칙: {len(targets)}개 / 전체: {len(all_rules)}개")

    target_rule_ids = {r["id"] for r, _ in targets}
    save_csv(all_rules, target_rule_ids, migrated_it_ids, migrated_cf_ids, blocks_link_type_id)
    save_original_jsons([r for r, _ in targets])

    fixed  = 0
    failed = 0
    for i, (rule, fix_types) in enumerate(targets, 1):
        rule_id   = rule["id"]
        rule_name = rule.get("name", "")
        print(f"[{i}/{len(targets)}] 처리 중: [{rule_id}] {rule_name} — fixTypes: {fix_types}")

        try:
            transformed = transform_rule(
                rule, fix_types,
                status_id_to_name, field_id_to_name,
                migrated_it_ids, migrated_cf_ids, migrated_cf_schemas,
                blocks_link_type_id,
            )

            if DRY_RUN:
                print(f"  [DRY-RUN] 업데이트 예정: [{rule_id}] {rule_name}")
                fixed += 1
                continue

            put_json(f"{base}/rule/{rule_id}", {"ruleConfigBean": transformed})
            fixed += 1

        except Exception as e:
            print(f"  [ERROR] 규칙 변환 실패: [{rule_id}] {rule_name} — {e}")
            failed += 1

    if DRY_RUN:
        print(f"[DRY-RUN] 자동화 규칙 업데이트 예정: {fixed}개")
    else:
        print(f"자동화 규칙 변환 완료: {fixed}/{len(targets)}개 성공, {failed}개 실패")


def rollback_automation_rules() -> None:
    """
    output/automation/{ruleId}_{ruleName}.json 파일 기반 일괄 롤백.
    """
    base          = automation_base()
    automation_dir = OUTPUT_DIR / "automation"

    if not automation_dir.exists():
        print(f"[WARN] 롤백 대상 디렉토리가 존재하지 않음: {automation_dir.resolve()}")
        return

    json_files = sorted(automation_dir.glob("*.json"))
    print(f"롤백 대상: {len(json_files)}개 파일 ({automation_dir.resolve()})")

    success = 0
    failed  = 0
    for file in json_files:
        file_name   = file.name
        rule_id_str = file_name.split("_")[0]
        rule_id     = int(rule_id_str)

        original_rule = json.loads(file.read_text(encoding="utf-8"))
        rule_name     = original_rule.get("name", "")
        print(f"롤백 중: [{rule_id}] {rule_name}")

        try:
            if DRY_RUN:
                print(f"  [DRY-RUN] 롤백 예정: [{rule_id}] {rule_name}")
                success += 1
                continue

            put_json(f"{base}/rule/{rule_id}", {"ruleConfigBean": strip_component_refs(original_rule)})
            success += 1

        except Exception as e:
            print(f"  [ERROR] 롤백 실패: [{rule_id}] {rule_name} — {e}")
            failed += 1

    if DRY_RUN:
        print(f"[DRY-RUN] 롤백 예정: {success}개")
    else:
        print(f"롤백 완료: {success}/{len(json_files)}개 성공, {failed}개 실패")


# ── 진입점 ────────────────────────────────────────────────────────────────────

def select_action() -> tuple[int, Optional[int]]:
    """(action, filter_rule_id) 반환. filter_rule_id 는 Fix 테스트 시에만 사용."""
    while True:
        print()
        print("Automation 작업을 선택하세요:")
        print("  1.   규칙 변환 (Fix) — 전체")
        print("  1 <rule_id>   규칙 변환 (Fix) — 특정 rule id 만 테스트")
        print("  2.   원본 롤백 (Rollback) — output/automation/ 의 원본 JSON 기준")
        choice = input(">>> ").strip()
        parts = choice.split()
        if parts and parts[0] == "1":
            if len(parts) == 2 and parts[1].isdigit():
                return 1, int(parts[1])
            if len(parts) == 1:
                return 1, None
        if choice == "2":
            return 2, None
        print("1, '1 <rule_id>', 또는 2를 입력하세요.")


def main() -> None:
    if not API_TOKEN:
        print("[ERROR] ATLASSIAN_API_TOKEN 환경 변수를 설정하세요.")
        sys.exit(1)

    print(f"Base URL : {BASE_URL}")
    print(f"Project  : {PROJECT_KEY}")
    print(f"Dry Run  : {DRY_RUN}")
    print(f"Output   : {OUTPUT_DIR.resolve()}")

    if not DRY_RUN:
        print()
        print("⚠  [WARNING] DRY_RUN=false — 실제 Jira 데이터에 변경이 적용됩니다.")
        confirm = input("계속하려면 'yes'를 입력하세요. 그 외 입력 시 종료합니다. >>> ").strip()
        if confirm.lower() != "yes":
            print("취소되었습니다.")
            sys.exit(0)

    print()
    print("=== Automation Migration Start ===")

    action, filter_rule_id = select_action()
    if action == 1:
        fix_automation_rules(filter_rule_id)
    else:
        rollback_automation_rules()

    print("=== Automation Migration Complete ===")


if __name__ == "__main__":
    main()
