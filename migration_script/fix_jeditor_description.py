#!/usr/bin/env python3
"""
Jira 자동화 규칙 - jeditor HTML description → 순수 텍스트 변환 도구

대상: description 필드에 jeditorTable 등 HTML이 포함된 자동화 규칙
처리: HTML 태그 제거, HTML 엔티티 디코딩, 순수 텍스트만 유지

사용법:
    export ATLASSIAN_API_TOKEN=your-token
    python3 fix_jeditor_description.py

환경 변수:
    ATLASSIAN_BASE_URL      기본값: https://sg-cf-migtest1.atlassian.net
    ATLASSIAN_EMAIL         기본값: jsjang@osci.kr
    ATLASSIAN_API_TOKEN     필수
    DRY_RUN                 기본값: true (false 로 설정해야 실제 변경)
    MAX_RETRIES             기본값: 5
    OUTPUT_DIR              기본값: ./output
"""

import base64
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

BASE_URL    = os.getenv("ATLASSIAN_BASE_URL", "https://sg-cf-usertest.atlassian.net")
EMAIL       = os.getenv("ATLASSIAN_EMAIL", "jsjang@osci.kr")
API_TOKEN   = os.getenv("ATLASSIAN_API_TOKEN", "")
DRY_RUN     = os.getenv("DRY_RUN", "false").lower() != "false"
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
OUTPUT_DIR  = Path(os.getenv("OUTPUT_DIR", "./output"))
PAGE_SIZE   = 100
AUTOMATION_PATH = "/gateway/api/automation/internal-api/jira/{cloud_id}/pro/rest/GLOBAL"


# ── HTTP 클라이언트 ───────────────────────────────────────────────────────────

def _auth_header() -> str:
    token = base64.b64encode(f"{EMAIL}:{API_TOKEN}".encode()).decode()
    return f"Basic {token}"


def _request(method: str, url: str, body: Any = None) -> Any:
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
            if (status == 429 or status >= 500) and attempt <= MAX_RETRIES:
                wait = 2 * attempt
                print(f"  [RETRY {attempt}/{MAX_RETRIES}] {status} — {wait}s 대기")
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


# ── Jira API ──────────────────────────────────────────────────────────────────

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


# ── HTML 처리 ─────────────────────────────────────────────────────────────────

# jeditor가 생성하는 HTML 클래스/태그 패턴
_JEDITOR_PATTERNS = [
    "jeditorTable",
    "jeditorTableWrapper",
    "jeditorTableCell",
]


def _is_jeditor_html(text: str) -> bool:
    """jeditor가 생성한 HTML인지 확인 (jeditor 클래스 또는 HTML 태그 포함 여부)."""
    if any(p in text for p in _JEDITOR_PATTERNS):
        return True
    # jeditor 외에도 일반 HTML 태그가 포함된 description 처리
    return bool(re.search(r'<(?:table|tr|td|th|p|b|br|ul|li|ol|span|div|h[1-6])[^>]*>', text, re.IGNORECASE))


def strip_html(html: str) -> str:
    """HTML 태그를 제거하고 순수 텍스트만 반환한다.

    - 블록 태그(</tr>, </p>, </li> 등) 앞에 줄바꿈 삽입
    - HTML 엔티티 디코딩
    - 연속 공백/빈 줄 정리
    """
    text = html

    # 블록 종료 태그 → 줄바꿈
    text = re.sub(r'</(?:tr|p|li|br|div|h[1-6])\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)

    # 모든 HTML 태그 제거
    text = re.sub(r'<[^>]+>', '', text)

    # HTML 엔티티 디코딩
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&amp;', '&')
    text = text.replace('&quot;', '"')
    text = text.replace('&#39;', "'")
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), text)

    # 연속 공백 → 단일 공백 (줄바꿈 보존)
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in text.split('\n')]

    # 빈 줄 3개 이상 → 최대 2개
    result_lines: list[str] = []
    blank_count = 0
    for line in lines:
        if line == '':
            blank_count += 1
            if blank_count <= 1:
                result_lines.append(line)
        else:
            blank_count = 0
            result_lines.append(line)

    return '\n'.join(result_lines).strip()


# ── 재귀 탐색 및 변환 ─────────────────────────────────────────────────────────

def _find_and_fix_description(node: Any, changes: list[str]) -> None:
    """
    재귀적으로 description 필드 operation을 탐색하여 HTML을 텍스트로 변환한다.

    대상 패턴:
      { "field": {"value": "description"}, ..., "value": {"value": "<html...>"} }
      또는
      { "fieldType": "description", ..., "value": "<html...>" }
    """
    if isinstance(node, dict):
        is_description_op = False

        field_obj = node.get("field")
        if isinstance(field_obj, dict) and field_obj.get("value") == "description":
            is_description_op = True

        if not is_description_op and node.get("fieldType") == "description":
            is_description_op = True

        if is_description_op:
            val = node.get("value")

            # 패턴 1: value가 dict이고 그 안의 "value"가 HTML 문자열
            if isinstance(val, dict):
                inner = val.get("value", "")
                if isinstance(inner, str) and _is_jeditor_html(inner):
                    clean = strip_html(inner)
                    val["value"] = clean
                    changes.append(f"value.value: {len(inner)} chars → {len(clean)} chars")

            # 패턴 2: value 자체가 HTML 문자열
            elif isinstance(val, str) and _is_jeditor_html(val):
                clean = strip_html(val)
                node["value"] = clean
                changes.append(f"value: {len(val)} chars → {len(clean)} chars")

        for v in node.values():
            _find_and_fix_description(v, changes)

    elif isinstance(node, list):
        for item in node:
            _find_and_fix_description(item, changes)


def has_jeditor_description(rule_json: str) -> bool:
    """규칙 JSON에 jeditor HTML description이 포함되어 있는지 빠르게 확인."""
    if not any(p in rule_json for p in _JEDITOR_PATTERNS):
        # jeditor 전용 클래스가 없어도 description 필드에 HTML이 있을 수 있음
        if '"value":"description"' not in rule_json and '"fieldType":"description"' not in rule_json:
            return False
        # description 필드가 있고 HTML 태그가 포함된 경우
        return bool(re.search(r'<(?:table|tr|td|th|p|b|br|ul|li|ol|span|div)[^>]*>', rule_json))
    return True


# ── 저장 / 롤백 전처리 ────────────────────────────────────────────────────────

BACKUP_DIR = OUTPUT_DIR / "jeditor_description"

# Jira는 PUT 후 컴포넌트 ID를 재생성하므로, 백업 JSON의 id/parentId 등을 제거해야 한다.
_COMPONENT_ID_FIELDS = {"id", "parentId", "conditionParentId", "checksum"}


def save_original(rule: dict) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    rule_id   = rule["id"]
    rule_name = re.sub(r'[\\/:*?"<>|]', "_", rule.get("name", ""))
    file_path = BACKUP_DIR / f"{rule_id}_{rule_name}.json"
    file_path.write_text(json.dumps(rule, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  백업 저장: {file_path.name}")


def strip_component_refs(rule: dict) -> dict:
    """롤백 PUT 전처리: 중첩 컴포넌트의 ID 참조 필드를 제거한다.
    최상위 rule id는 업데이트 대상 식별을 위해 유지한다.
    """
    import copy

    def _strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: _strip(v) for k, v in node.items() if k not in _COMPONENT_ID_FIELDS}
        if isinstance(node, list):
            return [_strip(item) for item in node]
        return node

    top_level_id = rule.get("id")
    result = _strip(copy.deepcopy(rule))
    if top_level_id is not None:
        result["id"] = top_level_id
    return result


# ── Fix ───────────────────────────────────────────────────────────────────────

def run_fix() -> None:
    base = automation_base()

    print("전체 자동화 규칙 조회 중...")
    all_rules = fetch_all_rules()
    print(f"전체 규칙: {len(all_rules)}개")

    targets: list[dict] = []
    for rule in all_rules:
        rule_json = json.dumps(rule, ensure_ascii=False, separators=(',', ':'))
        if has_jeditor_description(rule_json):
            targets.append(rule)

    print(f"jeditor HTML description 감지: {len(targets)}개 / 전체 {len(all_rules)}개\n")

    if not targets:
        print("변환 대상 규칙이 없습니다.")
        return

    import copy
    fixed = 0
    failed = 0

    for i, rule in enumerate(targets, 1):
        rule_id   = rule["id"]
        rule_name = rule.get("name", "")
        print(f"[{i}/{len(targets)}] [{rule_id}] {rule_name}")

        transformed = copy.deepcopy(rule)
        changes: list[str] = []
        _find_and_fix_description(transformed, changes)

        if not changes:
            print("  → description HTML 미감지 (스킵)\n")
            continue

        for change in changes:
            print(f"  변환: {change}")

        if DRY_RUN:
            print("  [DRY-RUN] 업데이트 예정\n")
            fixed += 1
            continue

        try:
            save_original(rule)
            put_json(f"{base}/rule/{rule_id}", {"ruleConfigBean": transformed})
            print("  업데이트 완료\n")
            fixed += 1
        except Exception as e:
            print(f"  [ERROR] 실패: {e}\n")
            failed += 1

    if DRY_RUN:
        print(f"[DRY-RUN] 변환 예정: {fixed}개")
    else:
        print(f"변환 완료: {fixed}/{len(targets)}개 성공, {failed}개 실패")
        if fixed > 0:
            print(f"원본 백업 위치: {BACKUP_DIR.resolve()}")


# ── Rollback ──────────────────────────────────────────────────────────────────

def run_rollback() -> None:
    if not BACKUP_DIR.exists():
        print(f"[ERROR] 백업 디렉토리가 존재하지 않습니다: {BACKUP_DIR.resolve()}")
        return

    json_files = sorted(BACKUP_DIR.glob("*.json"))
    if not json_files:
        print(f"[ERROR] 백업 파일이 없습니다: {BACKUP_DIR.resolve()}")
        return

    print(f"롤백 대상: {len(json_files)}개 파일 ({BACKUP_DIR.resolve()})\n")

    base = automation_base()
    success = 0
    failed  = 0

    for i, file in enumerate(json_files, 1):
        rule_id_str = file.name.split("_")[0]
        try:
            rule_id = int(rule_id_str)
        except ValueError:
            print(f"[{i}] [WARN] 파일명에서 rule id 파싱 실패: {file.name} (스킵)")
            continue

        original_rule = json.loads(file.read_text(encoding="utf-8"))
        rule_name     = original_rule.get("name", "")
        print(f"[{i}/{len(json_files)}] [{rule_id}] {rule_name}")

        if DRY_RUN:
            print("  [DRY-RUN] 롤백 예정\n")
            success += 1
            continue

        try:
            put_json(f"{base}/rule/{rule_id}", {"ruleConfigBean": strip_component_refs(original_rule)})
            print("  롤백 완료\n")
            success += 1
        except Exception as e:
            print(f"  [ERROR] 실패: {e}\n")
            failed += 1

    if DRY_RUN:
        print(f"[DRY-RUN] 롤백 예정: {success}개")
    else:
        print(f"롤백 완료: {success}/{len(json_files)}개 성공, {failed}개 실패")


# ── 진입점 ────────────────────────────────────────────────────────────────────

def select_action() -> int:
    while True:
        print("작업을 선택하세요:")
        print("  1. HTML → TEXT 변환 (Fix)")
        print("  2. 원본 복원 (Rollback) — output/jeditor_description/ 의 백업 JSON 기준")
        choice = input(">>> ").strip()
        if choice in ("1", "2"):
            return int(choice)
        print("1 또는 2를 입력하세요.\n")


def main() -> None:
    if not API_TOKEN:
        print("[ERROR] ATLASSIAN_API_TOKEN 환경 변수를 설정하세요.")
        sys.exit(1)

    print(f"Base URL : {BASE_URL}")
    print(f"Dry Run  : {DRY_RUN}")
    print(f"Output   : {OUTPUT_DIR.resolve()}")
    print()

    action = select_action()
    print()

    if not DRY_RUN:
        label = "변환" if action == 1 else "롤백"
        print(f"⚠  [WARNING] DRY_RUN=false — 실제 Jira 데이터에 {label}이 적용됩니다.")
        confirm = input("계속하려면 'yes'를 입력하세요. 그 외 입력 시 종료합니다. >>> ").strip()
        if confirm.lower() != "yes":
            print("취소되었습니다.")
            sys.exit(0)
        print()

    if action == 1:
        run_fix()
    else:
        run_rollback()


if __name__ == "__main__":
    main()
