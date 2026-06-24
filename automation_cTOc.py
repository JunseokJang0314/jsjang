#!/usr/bin/env python3
"""
Jira Cloud → Cloud 자동화 규칙 ID 재매핑 도구

export한 자동화 규칙을 다른 Jira Cloud 사이트로 import하면
커스텀 필드 ID / 이슈 타입 ID / 상태 ID 등이 불일치하여 오동작한다.
이 스크립트는 소스/타겟 사이트의 메타데이터를 비교하여
자동화 규칙에 박힌 모든 소스 ID를 타겟 ID로 교체한다.

매핑 대상:
  1. 커스텀 필드  customfield_XXXXX → customfield_YYYYY  (이름 기준 자동 매핑)
  2. 이슈 타입   소스 ID            → 타겟 ID             (이름 기준 자동 매핑)
  3. 상태        소스 ID            → 타겟 ID             (이름 기준 자동 매핑)

명시적 오버라이드 (자동 매핑 실패 시):
  FIELD_ID_OVERRIDE_MAP      커스텀 필드 ID 직접 지정
  ISSUE_TYPE_ID_OVERRIDE_MAP 이슈 타입 ID 직접 지정
  STATUS_ID_OVERRIDE_MAP     상태 ID 직접 지정

사용법:
    export SRC_API_TOKEN=...
    export TGT_API_TOKEN=...
    python3 automation_cTOc.py

환경 변수:
    SRC_BASE_URL    소스 사이트 URL        기본값: https://sg-cf-usertest.atlassian.net
    SRC_EMAIL       소스 사이트 계정 이메일  기본값: jsjang@osci.kr
    SRC_API_TOKEN   소스 사이트 API 토큰    필수

    TGT_BASE_URL    타겟 사이트 URL        기본값: https://sg-cf-migtest2.atlassian.net
    TGT_EMAIL       타겟 사이트 계정 이메일  기본값: jsjang@osci.kr
    TGT_API_TOKEN   타겟 사이트 API 토큰    필수

    TGT_PROJECT_KEY 타겟 프로젝트 키        기본값: SGCF
    DRY_RUN         true(기본)/false
    MAX_RETRIES     기본값: 5
    OUTPUT_DIR      기본값: ./output
"""

import base64
import copy
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

SRC_BASE_URL  = os.getenv("SRC_BASE_URL",  "https://sg-cf-usertest.atlassian.net")
SRC_EMAIL     = os.getenv("SRC_EMAIL",     "jsjang@osci.kr")
SRC_API_TOKEN = os.getenv("SRC_API_TOKEN", "")

TGT_BASE_URL    = os.getenv("TGT_BASE_URL",    "https://sg-cf.atlassian.net")
TGT_EMAIL       = os.getenv("TGT_EMAIL",       "jsjang@osci.kr")
TGT_API_TOKEN   = os.getenv("TGT_API_TOKEN",   "")
TGT_PROJECT_KEY = os.getenv("TGT_PROJECT_KEY", "SGCF")

DRY_RUN     = os.getenv("DRY_RUN", "true").lower() != "false"
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
OUTPUT_DIR  = Path(os.getenv("OUTPUT_DIR", "./output"))
PAGE_SIZE   = 100

AUTOMATION_PATH = "/gateway/api/automation/internal-api/jira/{cloud_id}/pro/rest/GLOBAL"

# ── 명시적 오버라이드 매핑 ────────────────────────────────────────────────────
# 이름 자동 매핑이 틀릴 때 여기에 직접 소스ID → 타겟ID 지정

# 커스텀 필드: { "customfield_XXXXX": "customfield_YYYYY" }
FIELD_ID_OVERRIDE_MAP: dict[str, str] = {
    # "customfield_10119": "customfield_10234",
}

# 이슈 타입: { "소스_이슈타입_ID": "타겟_이슈타입_ID" }
ISSUE_TYPE_ID_OVERRIDE_MAP: dict[str, str] = {
    # "10009": "10020",
}

# 상태: { "소스_상태_ID": "타겟_상태_ID" }
STATUS_ID_OVERRIDE_MAP: dict[str, str] = {
    # "10060": "10070",
}

# 링크 타입: { "소스_링크타입_ID": "타겟_링크타입_ID" }
LINK_TYPE_ID_OVERRIDE_MAP: dict[str, str] = {
    # "10010": "10008",
}


# ── HTTP 클라이언트 ───────────────────────────────────────────────────────────

def _auth_header(email: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def _retryable(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _request(method: str, url: str, email: str, token: str, body: Any = None) -> Any:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": _auth_header(email, token),
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
            status    = e.code
            body_text = e.read().decode("utf-8", errors="replace")[:500]
            if _retryable(status) and attempt <= MAX_RETRIES:
                wait = 2 * attempt
                print(f"  [RETRY {attempt}/{MAX_RETRIES}] {status} — {url} — {wait}s 대기")
                time.sleep(wait)
                continue
            raise RuntimeError(f"API 오류 {status}: {url}\n{body_text}")
    raise RuntimeError(f"최대 재시도 초과: {url}")


class JiraClient:
    def __init__(self, base_url: str, email: str, token: str, label: str = ""):
        self.base_url = base_url.rstrip("/")
        self.email    = email
        self.token    = token
        self.label    = label
        self._cloud_id: Optional[str] = None

    def _get(self, path: str) -> Any:
        return _request("GET", self.base_url + path, self.email, self.token)

    def _post(self, url: str, body: Any) -> Any:
        return _request("POST", url, self.email, self.token, body)

    def _put(self, url: str, body: Any) -> None:
        _request("PUT", url, self.email, self.token, body)

    def cloud_id(self) -> str:
        if not self._cloud_id:
            info = self._get("/_edge/tenant_info")
            self._cloud_id = info["cloudId"]
            print(f"[{self.label}] cloudId: {self._cloud_id}")
        return self._cloud_id

    def automation_base(self) -> str:
        return self.base_url + AUTOMATION_PATH.format(cloud_id=self.cloud_id())

    def fetch_fields(self) -> list[dict]:
        fields = self._get("/rest/api/3/field")
        print(f"[{self.label}] 필드 조회: {len(fields)}개")
        return fields

    def fetch_issue_types(self) -> list[dict]:
        types = self._get("/rest/api/3/issuetype")
        print(f"[{self.label}] 이슈 타입 조회: {len(types)}개")
        return types

    def fetch_statuses(self) -> list[dict]:
        statuses = self._get("/rest/api/3/status")
        print(f"[{self.label}] 상태 조회: {len(statuses)}개")
        return statuses

    def fetch_link_types(self) -> list[dict]:
        data = self._get("/rest/api/3/issueLinkType")
        link_types = data.get("issueLinkTypes", [])
        print(f"[{self.label}] 링크 타입 조회: {len(link_types)}개")
        return link_types

    def fetch_all_rules(self) -> list[dict]:
        all_rules: list[dict] = []
        offset = 0
        base = self.automation_base()
        while True:
            resp = self._post(f"{base}/rules", {"limit": PAGE_SIZE, "offset": offset})
            values = resp.get("values", [])
            if not values:
                break
            all_rules.extend(values)
            total  = resp.get("total", 0)
            offset += len(values)
            if offset >= total:
                break
        print(f"[{self.label}] 자동화 규칙 조회: {len(all_rules)}개")
        return all_rules

    def put_rule(self, rule_id: int, rule: dict) -> None:
        base = self.automation_base()
        self._put(f"{base}/rule/{rule_id}", {"ruleConfigBean": rule})


# ── 매핑 구성 ─────────────────────────────────────────────────────────────────

def _name_to_id(items: list[dict]) -> dict[str, str]:
    return {it["name"]: str(it["id"]) for it in items if it.get("name") and it.get("id")}


def _id_to_name(items: list[dict]) -> dict[str, str]:
    return {str(it["id"]): it["name"] for it in items if it.get("id") and it.get("name")}


def build_cf_id_map(src_fields: list[dict], tgt_fields: list[dict]) -> dict[str, str]:
    """커스텀 필드: 소스 ID → 타겟 ID (이름 기준 + 오버라이드)."""
    src_name_to_id = {
        f["name"]: f["id"]
        for f in src_fields
        if f.get("id", "").startswith("customfield_") and f.get("name")
    }
    tgt_name_to_id = {
        f["name"]: f["id"]
        for f in tgt_fields
        if f.get("id", "").startswith("customfield_") and f.get("name")
    }
    mapping: dict[str, str] = {}
    for name, src_id in src_name_to_id.items():
        tgt_id = tgt_name_to_id.get(name)
        if tgt_id and tgt_id != src_id:
            mapping[src_id] = tgt_id
    mapping.update(FIELD_ID_OVERRIDE_MAP)
    return mapping


def build_issuetype_id_map(
    src_types: list[dict], tgt_types: list[dict]
) -> dict[str, str]:
    """이슈 타입: 소스 ID → 타겟 ID (이름 기준 + 오버라이드)."""
    src_n2i = _name_to_id(src_types)
    tgt_n2i = _name_to_id(tgt_types)
    mapping: dict[str, str] = {}
    for name, src_id in src_n2i.items():
        tgt_id = tgt_n2i.get(name)
        if tgt_id and tgt_id != src_id:
            mapping[src_id] = tgt_id
    mapping.update(ISSUE_TYPE_ID_OVERRIDE_MAP)
    return mapping


def build_status_id_map(
    src_statuses: list[dict], tgt_statuses: list[dict]
) -> dict[str, str]:
    """상태: 소스 ID → 타겟 ID (이름 기준 + 오버라이드)."""
    src_n2i = _name_to_id(src_statuses)
    tgt_n2i = _name_to_id(tgt_statuses)
    mapping: dict[str, str] = {}
    for name, src_id in src_n2i.items():
        tgt_id = tgt_n2i.get(name)
        if tgt_id and tgt_id != src_id:
            mapping[src_id] = tgt_id
    mapping.update(STATUS_ID_OVERRIDE_MAP)
    return mapping


def build_linktype_id_map(
    src_links: list[dict], tgt_links: list[dict]
) -> dict[str, str]:
    """링크 타입: 소스 ID → 타겟 ID (name 기준 + 오버라이드).

    자동화 JSON에서 linkType은 "inward:ID" 또는 "outward:ID" 형식으로 저장된다.
    ID 부분만 교체하면 되므로 순수 숫자 ID 기준으로 매핑을 만든다.
    """
    src_n2i = _name_to_id(src_links)
    tgt_n2i = _name_to_id(tgt_links)
    mapping: dict[str, str] = {}
    for name, src_id in src_n2i.items():
        tgt_id = tgt_n2i.get(name)
        if tgt_id and tgt_id != src_id:
            mapping[src_id] = tgt_id
    mapping.update(LINK_TYPE_ID_OVERRIDE_MAP)
    return mapping


def build_src_id_to_name(
    src_fields: list[dict],
    src_types:  list[dict],
    src_statuses: list[dict],
    src_links: list[dict],
) -> dict[str, str]:
    result: dict[str, str] = {}
    result.update({f["id"]: f["name"] for f in src_fields if f.get("id") and f.get("name")})
    result.update(_id_to_name(src_types))
    result.update(_id_to_name(src_statuses))
    result.update(_id_to_name(src_links))
    return result


# ── 변환 함수 ─────────────────────────────────────────────────────────────────

def _replace_compare_value(
    cv: dict, id_map: dict[str, str], label: str
) -> None:
    """compareValue 노드에서 단일/다중 ID를 교체한다."""
    if not isinstance(cv, dict) or cv.get("type") != "ID":
        return
    if cv.get("multiValue") is True:
        try:
            ids: list[str] = json.loads(cv["value"])
            changed = False
            for i, old_id in enumerate(ids):
                new_id = id_map.get(str(old_id))
                if new_id:
                    print(f"  [{label}] compareValue(multi): {old_id} → {new_id}")
                    ids[i] = new_id
                    changed = True
            if changed:
                cv["value"] = json.dumps(ids)
        except (json.JSONDecodeError, TypeError):
            pass
    else:
        old_id = str(cv.get("value", ""))
        new_id = id_map.get(old_id)
        if new_id:
            print(f"  [{label}] compareValue: {old_id} → {new_id}")
            cv["value"] = new_id


def replace_ids_in_node(
    node:          Any,
    cf_map:        dict[str, str],
    issuetype_map: dict[str, str],
    status_map:    dict[str, str],
    linktype_map:  dict[str, str],
) -> None:
    """
    자동화 규칙 JSON을 재귀 탐색하며 모든 소스 ID를 타겟 ID로 교체한다.

    커스텀 필드 교체 위치:
      - operation.field.value        (Edit work item destination 필드)
      - COPY sourceField.value
      - selectedField.value          (커스텀 필드 조건)
      - type="field" 노드의 value    (트리거 필드 참조)

    이슈 타입 교체 위치:
      - jira.issue.type 브랜치의 issueTypes 배열
      - selectedField.value="issuetype" 인 compareValue
      - operation.field.value="issuetype" 인 operation value

    상태 교체 위치:
      - selectedField.value="status" 인 compareValue
      - jira.issue.transitioned 트리거의 fromStatus / toStatus
      - jira.issue.status 조건의 statusIds 배열

    링크 타입 교체 위치:
      - jira.issue.link 액션의 linkType ("inward:ID" / "outward:ID")
    """
    if not isinstance(node, dict):
        if isinstance(node, list):
            for item in node:
                replace_ids_in_node(item, cf_map, issuetype_map, status_map, linktype_map)
        return

    field_obj = node.get("field")

    # ── 커스텀 필드 ────────────────────────────────────────────────────────────

    # Edit work item destination: {"field": {"type": "ID", "value": "customfield_..."}}
    if isinstance(field_obj, dict) and field_obj.get("type") == "ID":
        fv = field_obj.get("value", "")
        if fv in cf_map:
            print(f"  [CF-DEST]  field.value {fv} → {cf_map[fv]}")
            field_obj["value"] = cf_map[fv]

    # COPY sourceField
    sf = node.get("sourceField")
    if isinstance(sf, dict) and sf.get("type") == "ID":
        sv = sf.get("value", "")
        if sv in cf_map:
            print(f"  [CF-SRC]   sourceField {sv} → {cf_map[sv]}")
            sf["value"] = cf_map[sv]

    # 트리거 field 참조: {"type": "field", "value": "customfield_..."}
    if node.get("type") == "field":
        v = node.get("value", "")
        if v in cf_map:
            print(f"  [CF-TRIG]  field value {v} → {cf_map[v]}")
            node["value"] = cf_map[v]

    # ── 이슈 타입 ──────────────────────────────────────────────────────────────

    # jira.issue.type 브랜치: {"type": "jira.issue.type", "value": {"issueTypes": [...]}}
    if node.get("type") == "jira.issue.type":
        value_obj = node.get("value")
        if isinstance(value_obj, dict):
            types = value_obj.get("issueTypes", [])
            changed = False
            for i, old_id in enumerate(types):
                new_id = issuetype_map.get(str(old_id))
                if new_id:
                    print(f"  [IT-BRANCH] issueTypes[{i}]: {old_id} → {new_id}")
                    types[i] = new_id
                    changed = True
            if changed:
                value_obj["issueTypes"] = types

    # Edit/Create 액션에서 issuetype 값 교체
    # {"field": {"value": "issuetype"}, "value": {"type": "ID", "value": "10009"}}
    if (isinstance(field_obj, dict)
            and field_obj.get("value") == "issuetype"
            and isinstance(node.get("value"), dict)):
        op_val = node["value"]
        if op_val.get("type") == "ID":
            old_id = str(op_val.get("value", ""))
            new_id = issuetype_map.get(old_id)
            if new_id:
                print(f"  [IT-OP]    operation issuetype {old_id} → {new_id}")
                op_val["value"] = new_id

    # ── 상태: Work item transitioned 트리거 ────────────────────────────────────
    # 가능한 구조:
    #   A) {"fromStatus": {"type": "ID", "value": "id"}, ...}
    #   B) {"fromStatus": {"value": "id"}, ...}          (type 필드 없음)
    #   C) {"fromStatus": "id", ...}                     (문자열)
    if node.get("type") == "jira.issue.transitioned":
        value_obj = node.get("value")
        if isinstance(value_obj, dict):
            for key in ("fromStatus", "toStatus"):
                s = value_obj.get(key)
                if isinstance(s, dict):
                    # type이 NAME이면 이미 이름으로 저장된 것 → ID 교체 불필요
                    if s.get("type") == "NAME":
                        pass
                    else:
                        # type이 ID이거나 type 필드 없는 경우 모두 처리
                        old_id = str(s.get("value", ""))
                        new_id = status_map.get(old_id)
                        if new_id:
                            print(f"  [ST-TRIG]  {key}: {old_id} → {new_id}")
                            s["value"] = new_id
                elif isinstance(s, str):
                    # 문자열로 직접 저장된 경우
                    new_id = status_map.get(s)
                    if new_id:
                        print(f"  [ST-TRIG]  {key}: {s} → {new_id}")
                        value_obj[key] = new_id

    # ── 상태: jira.issue.status 조건의 statusIds 배열 ──────────────────────────
    # {"type": "jira.issue.status", "value": {"statusIds": ["10001", "10002"]}}
    if node.get("type") == "jira.issue.status":
        value_obj = node.get("value")
        if isinstance(value_obj, dict):
            status_ids = value_obj.get("statusIds", [])
            changed = False
            for i, old_id in enumerate(status_ids):
                new_id = status_map.get(str(old_id))
                if new_id:
                    print(f"  [ST-COND2] statusIds[{i}]: {old_id} → {new_id}")
                    status_ids[i] = new_id
                    changed = True
            if changed:
                value_obj["statusIds"] = status_ids

    # ── 링크 타입 ──────────────────────────────────────────────────────────────
    # jira.issue.link 액션: {"linkType": "inward:10010"} 또는 {"linkType": "outward:10010"}
    if node.get("type") == "jira.issue.link":
        value_obj = node.get("value")
        if isinstance(value_obj, dict):
            lt = value_obj.get("linkType", "")
            if isinstance(lt, str) and ":" in lt:
                direction, old_id = lt.split(":", 1)
                new_id = linktype_map.get(old_id)
                if new_id:
                    print(f"  [LT]       linkType {lt} → {direction}:{new_id}")
                    value_obj["linkType"] = f"{direction}:{new_id}"

    # ── 컨텍스트 기반 compareValue 교체 ───────────────────────────────────────
    sel = node.get("selectedField")
    cv  = node.get("compareValue")
    if isinstance(sel, dict) and isinstance(cv, dict):
        sel_val = sel.get("value", "")

        if sel_val == "issuetype":
            _replace_compare_value(cv, issuetype_map, "IT-COND")

        elif sel_val == "status":
            _replace_compare_value(cv, status_map, "ST-COND")

        elif str(sel_val).startswith("customfield_") and sel.get("type") == "ID":
            if sel_val in cf_map:
                print(f"  [CF-COND]  selectedField {sel_val} → {cf_map[sel_val]}")
                sel["value"] = cf_map[sel_val]

    # 재귀
    for v in node.values():
        replace_ids_in_node(v, cf_map, issuetype_map, status_map, linktype_map)


def _str_replace_cf(rule_json: str, cf_map: dict[str, str]) -> str:
    """구조 탐색 후에도 남은 커스텀 필드 ID를 문자열 치환으로 처리.

    순차 치환 시 A→B, B→C 체인으로 이중 치환되는 문제를 막기 위해
    모든 소스 ID를 단일 패스(regex alternation)로 한 번에 교체한다.
    """
    active = {k: v for k, v in cf_map.items() if k in rule_json}
    if not active:
        return rule_json
    pattern = re.compile("|".join(re.escape(k) for k in active))
    def _replacer(m: re.Match) -> str:
        src_id = m.group(0)
        tgt_id = active[src_id]
        print(f"  [CF-STR]   {src_id} → {tgt_id}")
        return tgt_id
    return pattern.sub(_replacer, rule_json)


def _str_replace_status(rule_json: str, status_map: dict[str, str]) -> str:
    """
    구조 탐색으로 못 잡은 status ID를 JSON 문자열에서 직접 교체한다.

    JSON 내에서 status ID가 나타날 수 있는 패턴:
      "value":"ID"   →  "value":"NEW_ID"
      "value":ID     →  "value":NEW_ID    (정수 저장 시)
      "id":"ID"      →  "id":"NEW_ID"
      "id":ID        →  "id":NEW_ID
    커스텀 필드 ID(customfield_*)와 달리 status ID는 순수 숫자라
    quoted string 경계 또는 콜론+숫자+구분자 패턴으로 치환한다.

    체인 치환(A→B→C) 방지를 위해 quoted/unquoted 각각 단일 패스로 처리한다.
    """
    # quoted: "10050" → "10070"
    active_q = {k: v for k, v in status_map.items() if f'"{k}"' in rule_json}
    if active_q:
        pattern_q = re.compile("|".join(f'"{re.escape(k)}"' for k in active_q))
        def _rep_q(m: re.Match) -> str:
            src_id = m.group(0)[1:-1]  # strip quotes
            tgt_id = active_q[src_id]
            print(f"  [ST-STR]   {src_id} → {tgt_id}")
            return f'"{tgt_id}"'
        rule_json = pattern_q.sub(_rep_q, rule_json)

    # 정수: :10050[,}]] → :10070[,}]]
    active_i = {k: v for k, v in status_map.items()
                if any(f':{k}{s}' in rule_json for s in (',', '}', ']'))}
    if active_i:
        pattern_i = re.compile(
            r':(' + "|".join(re.escape(k) for k in active_i) + r')([,}\]])'
        )
        def _rep_i(m: re.Match) -> str:
            src_id = m.group(1)
            tgt_id = active_i[src_id]
            print(f"  [ST-STR]   {src_id} → {tgt_id} (int)")
            return f':{tgt_id}{m.group(2)}'
        rule_json = pattern_i.sub(_rep_i, rule_json)

    return rule_json


def transform_rule(
    rule:          dict,
    cf_map:        dict[str, str],
    issuetype_map: dict[str, str],
    status_map:    dict[str, str],
    linktype_map:  dict[str, str],
) -> dict:
    result    = copy.deepcopy(rule)
    rule_json = json.dumps(result, ensure_ascii=False)

    # 이 규칙에 실제로 등장하는 소스 ID만 필터링
    active_cf = {k: v for k, v in cf_map.items()        if k in rule_json}
    active_it = {k: v for k, v in issuetype_map.items() if k in rule_json}
    # status는 순수 숫자라 quoted 형태("ID")로 존재 여부 확인
    active_st = {k: v for k, v in status_map.items() if f'"{k}"' in rule_json or f':{k},' in rule_json or f':{k}}}' in rule_json}
    # linktype은 "inward:ID" / "outward:ID" 형태로 존재 여부 확인
    active_lt = {k: v for k, v in linktype_map.items()
                 if f'inward:{k}' in rule_json or f'outward:{k}' in rule_json}

    if not (active_cf or active_it or active_st or active_lt):
        return result

    # 1) 구조 기반 교체 (알려진 JSON 패턴)
    replace_ids_in_node(result, active_cf, active_it, active_st, active_lt)

    # 2) 문자열 치환 — 구조 탐색 후에도 여전히 남아 있는 소스 ID만 대상으로 한다.
    #    주의: active_cf의 target값(예: A→B, B→C 에서 B)은 구조 치환이 방금 올바르게
    #    배치한 값이므로, 이를 다시 치환하면 이중 치환이 발생한다.
    #    → target ID set에 속하는 key는 remaining_cf에서 제외한다.
    rule_json = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
    cf_target_ids = set(active_cf.values())
    remaining_cf = {k: v for k, v in active_cf.items()
                    if k in rule_json and k not in cf_target_ids}
    remaining_st = {k: v for k, v in active_st.items()
                    if f'"{k}"' in rule_json or f':{k},' in rule_json or f':{k}}}' in rule_json}
    if remaining_cf:
        rule_json = _str_replace_cf(rule_json, remaining_cf)
    if remaining_st:
        rule_json = _str_replace_status(rule_json, remaining_st)
    result = json.loads(rule_json)

    return result


# ── 저장 ──────────────────────────────────────────────────────────────────────

def save_original_jsons(rules: list[dict]) -> None:
    automation_dir = OUTPUT_DIR / "automation_cTOc"
    automation_dir.mkdir(parents=True, exist_ok=True)
    for rule in rules:
        rule_id   = rule["id"]
        rule_name = re.sub(r'[\\/:*?"<>|]', "_", rule.get("name", ""))
        path = automation_dir / f"{rule_id}_{rule_name}.json"
        path.write_text(json.dumps(rule, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"원본 JSON 저장 완료: {len(rules)}개 → {(OUTPUT_DIR / 'automation_cTOc').resolve()}")


def _log_and_collect_mapping(
    title: str,
    id_map: dict[str, str],
    src_id_to_name: dict[str, str],
    tgt_id_to_name: dict[str, str],
    override_map: dict[str, str],
) -> list[list]:
    print(f"\n[{title}] {len(id_map)}개")
    rows = []
    for src_id, tgt_id in sorted(id_map.items()):
        method   = "override" if src_id in override_map else "name"
        src_name = src_id_to_name.get(src_id, "?")
        tgt_name = tgt_id_to_name.get(tgt_id, "?")
        print(f"  {src_id} ({src_name}) → {tgt_id} ({tgt_name})  [{method}]")
        rows.append([title, src_id, src_name, tgt_id, tgt_name, method])
    return rows


def save_mapping_csv(
    cf_map:         dict[str, str],
    issuetype_map:  dict[str, str],
    status_map:     dict[str, str],
    linktype_map:   dict[str, str],
    src_id_to_name: dict[str, str],
    tgt_id_to_name: dict[str, str],
) -> None:
    path = OUTPUT_DIR / "field_id_mapping.csv"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[list] = []
    rows += _log_and_collect_mapping("커스텀필드",  cf_map,        src_id_to_name, tgt_id_to_name, FIELD_ID_OVERRIDE_MAP)
    rows += _log_and_collect_mapping("이슈타입",   issuetype_map,  src_id_to_name, tgt_id_to_name, ISSUE_TYPE_ID_OVERRIDE_MAP)
    rows += _log_and_collect_mapping("상태",       status_map,     src_id_to_name, tgt_id_to_name, STATUS_ID_OVERRIDE_MAP)
    rows += _log_and_collect_mapping("링크타입",   linktype_map,   src_id_to_name, tgt_id_to_name, LINK_TYPE_ID_OVERRIDE_MAP)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["매핑종류", "소스ID", "소스이름", "타겟ID", "타겟이름", "매핑방식"])
        writer.writerows(rows)
    print(f"\n매핑 CSV 저장: {path.resolve()}")


def save_result_csv(
    all_rules:     list[dict],
    fixed_ids:     set[int],
    cf_map:        dict[str, str],
    issuetype_map: dict[str, str],
    status_map:    dict[str, str],
) -> None:
    path = OUTPUT_DIR / "automation_cTOc_result.csv"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    all_src_ids = set(cf_map) | set(issuetype_map) | set(status_map)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["RuleId", "RuleName", "교체여부", "교체된_소스ID목록"])
        for rule in all_rules:
            rule_id   = rule["id"]
            rule_name = rule.get("name", "")
            if rule_id in fixed_ids:
                rule_json = json.dumps(rule, ensure_ascii=False)
                matched   = sorted(sid for sid in all_src_ids if sid in rule_json)
                writer.writerow([rule_id, rule_name, "O", ", ".join(matched)])
            else:
                writer.writerow([rule_id, rule_name, "", ""])
    print(f"결과 CSV 저장: {path.resolve()}")


# ── Rollback ──────────────────────────────────────────────────────────────────

_COMPONENT_ID_FIELDS = {"id", "parentId", "conditionParentId", "checksum"}


def strip_component_refs(rule: dict) -> dict:
    def _strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: _strip(v) for k, v in node.items() if k not in _COMPONENT_ID_FIELDS}
        if isinstance(node, list):
            return [_strip(item) for item in node]
        return node

    top_id = rule.get("id")
    result = _strip(copy.deepcopy(rule))
    if top_id is not None:
        result["id"] = top_id
    return result


# ── Fix / Rollback 진입점 ─────────────────────────────────────────────────────

def fix_rules(filter_rule_id: Optional[int] = None) -> None:
    src = JiraClient(SRC_BASE_URL, SRC_EMAIL, SRC_API_TOKEN, label="SRC")
    tgt = JiraClient(TGT_BASE_URL, TGT_EMAIL, TGT_API_TOKEN, label="TGT")

    print("\n── 메타데이터 조회 ──────────────────────────────────────")
    src_fields   = src.fetch_fields()
    src_types    = src.fetch_issue_types()
    src_statuses = src.fetch_statuses()
    src_links    = src.fetch_link_types()
    tgt_fields   = tgt.fetch_fields()
    tgt_types    = tgt.fetch_issue_types()
    tgt_statuses = tgt.fetch_statuses()
    tgt_links    = tgt.fetch_link_types()

    cf_map        = build_cf_id_map(src_fields, tgt_fields)
    issuetype_map = build_issuetype_id_map(src_types, tgt_types)
    status_map    = build_status_id_map(src_statuses, tgt_statuses)
    linktype_map  = build_linktype_id_map(src_links, tgt_links)

    if not (cf_map or issuetype_map or status_map or linktype_map):
        print("[INFO] 교체 대상 ID가 없습니다.")
        print("       이름이 다를 경우 *_OVERRIDE_MAP 에 직접 지정하세요.")
        return

    src_id_to_name = build_src_id_to_name(src_fields, src_types, src_statuses, src_links)
    tgt_id_to_name = {f["id"]: f["name"] for f in tgt_fields if f.get("id") and f.get("name")}
    tgt_id_to_name.update(_id_to_name(tgt_types))
    tgt_id_to_name.update(_id_to_name(tgt_statuses))
    tgt_id_to_name.update(_id_to_name(tgt_links))

    save_mapping_csv(cf_map, issuetype_map, status_map, linktype_map, src_id_to_name, tgt_id_to_name)

    print("\n── 자동화 규칙 조회 ─────────────────────────────────────")
    all_rules = tgt.fetch_all_rules()

    if filter_rule_id is not None:
        all_rules = [r for r in all_rules if r["id"] == filter_rule_id]
        if not all_rules:
            print(f"[ERROR] rule id={filter_rule_id} 를 찾을 수 없습니다.")
            return
        print(f"테스트 모드: rule id={filter_rule_id} 만 처리합니다.")

    all_src_ids = set(cf_map) | set(issuetype_map) | set(status_map)
    lt_src_ids  = set(linktype_map)
    targets = [
        r for r in all_rules
        if any(sid in json.dumps(r, ensure_ascii=False) for sid in all_src_ids)
        or any(f'inward:{sid}' in json.dumps(r, ensure_ascii=False)
               or f'outward:{sid}' in json.dumps(r, ensure_ascii=False)
               for sid in lt_src_ids)
    ]

    print(f"\n변환 대상: {len(targets)}개 / 전체: {len(all_rules)}개")
    save_original_jsons(targets)
    save_result_csv(all_rules, {r["id"] for r in targets}, cf_map, issuetype_map, status_map)

    fixed  = 0
    failed = 0
    print("\n── 변환 시작 ────────────────────────────────────────────")
    for i, rule in enumerate(targets, 1):
        rule_id   = rule["id"]
        rule_name = rule.get("name", "")
        print(f"\n[{i}/{len(targets)}] [{rule_id}] {rule_name}")

        try:
            transformed = transform_rule(rule, cf_map, issuetype_map, status_map, linktype_map)

            if DRY_RUN:
                print(f"  [DRY-RUN] 업데이트 예정")
                fixed += 1
                continue

            tgt.put_rule(rule_id, transformed)
            fixed += 1

        except Exception as e:
            print(f"  [ERROR] {e}")
            failed += 1

    print()
    if DRY_RUN:
        print(f"[DRY-RUN] 업데이트 예정: {fixed}개")
    else:
        print(f"변환 완료: {fixed}/{len(targets)}개 성공, {failed}개 실패")


def _try_put_rule(tgt: JiraClient, rule_id: int, body: dict) -> None:
    """PUT을 시도하고 실패 시 RuntimeError를 발생시킨다."""
    tgt.put_rule(rule_id, body)


def rollback_rules() -> None:
    """
    output/automation_cTOc/ 의 원본 JSON 기반 일괄 롤백.

    롤백 전략 (순서대로 시도):
      1단계: 원본 JSON을 그대로 PUT (컴포넌트 ID 유지)
      2단계: 실패 시 strip_component_refs 후 PUT (ID 제거 → Jira 재할당)
      → 모두 실패 시: 원본 JSON에 소스 사이트의 깨진 필드 참조가 포함되어
                      API가 검증을 통과시키지 못하는 경우로, API를 통한 롤백 불가.
                      Fix를 다시 실행하거나 UI에서 수동으로 수정하세요.
    """
    tgt = JiraClient(TGT_BASE_URL, TGT_EMAIL, TGT_API_TOKEN, label="TGT")
    automation_dir = OUTPUT_DIR / "automation_cTOc"

    if not automation_dir.exists():
        print(f"[WARN] 롤백 디렉토리 없음: {automation_dir.resolve()}")
        return

    json_files = sorted(automation_dir.glob("*.json"))
    print(f"롤백 대상: {len(json_files)}개 파일 ({automation_dir.resolve()})")

    success      = 0
    failed       = 0
    api_rejected = 0

    for i, file in enumerate(json_files, 1):
        rule_id   = int(file.name.split("_")[0])
        original  = json.loads(file.read_text(encoding="utf-8"))
        rule_name = original.get("name", "")
        print(f"\n[{i}/{len(json_files)}] [{rule_id}] {rule_name}")

        if DRY_RUN:
            print(f"  [DRY-RUN] 롤백 예정")
            success += 1
            continue

        # 1단계: 원본 그대로 PUT
        try:
            _try_put_rule(tgt, rule_id, original)
            print(f"  [OK] 롤백 성공 (원본 ID 유지)")
            success += 1
            continue
        except RuntimeError as e1:
            err1 = str(e1)

        # 2단계: strip 후 PUT
        try:
            _try_put_rule(tgt, rule_id, strip_component_refs(original))
            print(f"  [OK] 롤백 성공 (컴포넌트 ID strip)")
            success += 1
            continue
        except RuntimeError as e2:
            err2 = str(e2)

        # 두 시도 모두 실패
        if "400" in err2 and ("valid value" in err2 or "errorMessages" in err2):
            print(f"  [SKIP] API 검증 거부 — 원본 JSON에 소스 사이트의 깨진 필드 참조 포함.")
            print(f"         Fix를 다시 실행하거나 UI에서 수동으로 수정하세요.")
            api_rejected += 1
        else:
            print(f"  [ERROR] 1단계: {err1[:200]}")
            print(f"  [ERROR] 2단계: {err2[:200]}")
            failed += 1

    total = len(json_files)
    print(f"\n롤백 완료: {success}/{total}개 성공"
          + (f", {api_rejected}개 API 거부 (수동 처리 필요)" if api_rejected else "")
          + (f", {failed}개 기타 오류" if failed else ""))


# ── 활성화 동기화 ─────────────────────────────────────────────────────────────

def enable_rules() -> None:
    """
    소스 사이트에서 ENABLED 상태인 룰을 조회하고,
    같은 이름의 룰을 타겟 사이트에서 찾아 ENABLED로 변경한다.

    매핑 기준: 룰 이름 (name) 일치
    """
    src = JiraClient(SRC_BASE_URL, SRC_EMAIL, SRC_API_TOKEN, label="SRC")
    tgt = JiraClient(TGT_BASE_URL, TGT_EMAIL, TGT_API_TOKEN, label="TGT")

    print("\n── 자동화 규칙 조회 ─────────────────────────────────────")
    src_rules = src.fetch_all_rules()
    tgt_rules = tgt.fetch_all_rules()

    src_enabled_names = {
        r["name"] for r in src_rules if r.get("state") == "ENABLED"
    }
    print(f"\n소스 ENABLED 룰: {len(src_enabled_names)}개 / 전체: {len(src_rules)}개")

    # 타겟에서 이름으로 룰 찾기
    tgt_by_name: dict[str, dict] = {}
    for r in tgt_rules:
        name = r.get("name", "")
        if name not in tgt_by_name:
            tgt_by_name[name] = r

    targets = [
        r for r in tgt_rules
        if r.get("name") in src_enabled_names and r.get("state") != "ENABLED"
    ]
    already_enabled = sum(
        1 for r in tgt_rules
        if r.get("name") in src_enabled_names and r.get("state") == "ENABLED"
    )
    not_found = [
        name for name in src_enabled_names if name not in tgt_by_name
    ]

    print(f"타겟 활성화 대상: {len(targets)}개  (이미 ENABLED: {already_enabled}개)")
    if not_found:
        print(f"타겟에 없는 룰 ({len(not_found)}개):")
        for name in sorted(not_found):
            print(f"  - {name}")

    if not targets:
        print("활성화할 룰이 없습니다.")
        return

    success = 0
    failed  = 0
    print("\n── 활성화 시작 ──────────────────────────────────────────")
    for i, rule in enumerate(targets, 1):
        rule_id   = rule["id"]
        rule_name = rule.get("name", "")
        print(f"\n[{i}/{len(targets)}] [{rule_id}] {rule_name}")

        if DRY_RUN:
            print(f"  [DRY-RUN] ENABLED 예정")
            success += 1
            continue

        try:
            updated = copy.deepcopy(rule)
            updated["state"] = "ENABLED"
            tgt.put_rule(rule_id, updated)
            print(f"  [OK] ENABLED")
            success += 1
        except Exception as e:
            print(f"  [ERROR] {e}")
            failed += 1

    print()
    if DRY_RUN:
        print(f"[DRY-RUN] 활성화 예정: {success}개")
    else:
        print(f"활성화 완료: {success}/{len(targets)}개 성공, {failed}개 실패")


# ── 전체 비활성화 ─────────────────────────────────────────────────────────────

def disable_all_rules() -> None:
    """타겟 사이트의 모든 ENABLED 룰을 DISABLED로 변경한다."""
    tgt = JiraClient(TGT_BASE_URL, TGT_EMAIL, TGT_API_TOKEN, label="TGT")

    print("\n── 자동화 규칙 조회 ─────────────────────────────────────")
    all_rules = tgt.fetch_all_rules()

    targets = [r for r in all_rules if r.get("state") == "ENABLED"]
    print(f"\n비활성화 대상: {len(targets)}개 / 전체: {len(all_rules)}개")

    if not targets:
        print("ENABLED 룰이 없습니다.")
        return

    success = 0
    failed  = 0
    print("\n── 비활성화 시작 ────────────────────────────────────────")
    for i, rule in enumerate(targets, 1):
        rule_id   = rule["id"]
        rule_name = rule.get("name", "")
        print(f"\n[{i}/{len(targets)}] [{rule_id}] {rule_name}")

        if DRY_RUN:
            print(f"  [DRY-RUN] DISABLED 예정")
            success += 1
            continue

        try:
            updated = copy.deepcopy(rule)
            updated["state"] = "DISABLED"
            tgt.put_rule(rule_id, updated)
            print(f"  [OK] DISABLED")
            success += 1
        except Exception as e:
            print(f"  [ERROR] {e}")
            failed += 1

    print()
    if DRY_RUN:
        print(f"[DRY-RUN] 비활성화 예정: {success}개")
    else:
        print(f"비활성화 완료: {success}/{len(targets)}개 성공, {failed}개 실패")


# ── 진입점 ────────────────────────────────────────────────────────────────────

def select_action() -> tuple[int, Optional[int]]:
    while True:
        print()
        print("작업을 선택하세요:")
        print("  1            ID 재매핑 (Fix) — 전체")
        print("  1 <rule_id>  ID 재매핑 (Fix) — 특정 rule id 테스트")
        print("  2            원본 롤백 (Rollback) — output/automation_cTOc/ 기준")
        print("  3            소스 ENABLED 룰 → 타겟 활성화 동기화")
        print("  4            타겟 전체 룰 비활성화")
        choice = input(">>> ").strip()
        parts  = choice.split()
        if parts and parts[0] == "1":
            if len(parts) == 2 and parts[1].isdigit():
                return 1, int(parts[1])
            if len(parts) == 1:
                return 1, None
        if choice == "2":
            return 2, None
        if choice == "3":
            return 3, None
        if choice == "4":
            return 4, None
        print("1, '1 <rule_id>', 2, 3, 또는 4를 입력하세요.")


def main() -> None:
    if not SRC_API_TOKEN:
        print("[ERROR] SRC_API_TOKEN 환경 변수를 설정하세요.")
        sys.exit(1)
    if not TGT_API_TOKEN:
        print("[ERROR] TGT_API_TOKEN 환경 변수를 설정하세요.")
        sys.exit(1)

    print(f"소스 사이트 : {SRC_BASE_URL}")
    print(f"타겟 사이트 : {TGT_BASE_URL}  (프로젝트: {TGT_PROJECT_KEY})")
    print(f"Dry Run    : {DRY_RUN}")
    print(f"Output     : {OUTPUT_DIR.resolve()}")

    if not DRY_RUN:
        print()
        print("⚠  [WARNING] DRY_RUN=false — 타겟 사이트 자동화 규칙이 실제로 변경됩니다.")
        confirm = input("계속하려면 'yes'를 입력하세요. >>> ").strip()
        if confirm.lower() != "yes":
            print("취소되었습니다.")
            sys.exit(0)

    print()
    print("=== Automation Cloud-to-Cloud ID Remapping Start ===")

    action, filter_rule_id = select_action()
    if action == 1:
        fix_rules(filter_rule_id)
    elif action == 2:
        rollback_rules()
    elif action == 3:
        enable_rules()
    else:
        disable_all_rules()

    print("=== Done ===")


if __name__ == "__main__":
    main()
