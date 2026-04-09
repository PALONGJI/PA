import re
import os
import json
import html
from pathlib import Path
from collections import defaultdict

import fitz  # PyMuPDF
from flask import Flask, render_template, request, redirect, url_for, flash

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = "oa-mapper-secret-key"


def extract_text_from_pdf(pdf_path: Path) -> str:
    text_parts = []
    doc = fitz.open(pdf_path)
    for page in doc:
        text_parts.append(page.get_text("text"))
    doc.close()
    return "\n".join(text_parts)


def read_uploaded_file_text(file_path: Path) -> str:
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        return extract_text_from_pdf(file_path)

    if suffix in [".txt"]:
        return file_path.read_text(encoding="utf-8")

    raise ValueError(f"지원하지 않는 파일 형식입니다: {suffix}")


def clean_claim_body(text: str) -> str:
    text = text.strip()

    # 맨 앞 청구항 헤더 제거
    text = re.sub(
        r'^\s*청구항\s*(?:제\s*)?\d+(?:\s*항)?\s*',
        '',
        text
    ).strip()

    # 줄 단위로 쪼개서 괄호 찌꺼기만 있는 줄 제거
    cleaned_lines = []
    for line in text.splitlines():
        line = line.strip()

        # [, ], 【, 】 같은 문자만 있는 줄 제거
        if re.fullmatch(r'[\[\]【】〔〕()（）「」『』]+', line):
            continue

        cleaned_lines.append(line)

    text = "\n".join(cleaned_lines).strip()

    # 앞뒤 괄호 찌꺼기 제거
    text = re.sub(r'^[\[\]【】〔〕()（）「」『』\s]+', '', text)
    text = re.sub(r'[\[\]【】〔〕()（）「」『』\s]+$', '', text)

    return text.strip()


def split_claims(claim_text: str):
    """
    실제 청구항 헤더만 기준으로 청구항 분리
    """
    normalized = claim_text.replace("\r\n", "\n").replace("\r", "\n")

    normalized = re.sub(
        r'(?<!^)(\s*)(청구항\s*(?:제\s*)?\d+(?:\s*항)?)',
        r'\n\2',
        normalized
    )

    lines = normalized.split("\n")
    claims = []

    current_claim_no = None
    current_lines = []

    header_pattern = re.compile(r'^\s*청구항\s*(?:제\s*)?(\d+)(?:\s*항)?\b')

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        m = header_pattern.match(stripped)

        if m:
            if current_claim_no is not None:
                full_text = "\n".join(current_lines).strip()
                body_text = clean_claim_body(full_text)

                claims.append({
                    "claim_no": current_claim_no,
                    "title": f"[청구항 {current_claim_no}]",
                    "text": body_text,
                    "rejections": []
                })

            current_claim_no = int(m.group(1))
            current_lines = [stripped]
        else:
            if current_claim_no is not None:
                current_lines.append(stripped)

    if current_claim_no is not None:
        full_text = "\n".join(current_lines).strip()
        body_text = clean_claim_body(full_text)

        claims.append({
            "claim_no": current_claim_no,
            "title": f"[청구항 {current_claim_no}]",
            "text": body_text,
            "rejections": []
        })

    claims.sort(key=lambda x: x["claim_no"])

    unique_claims = []
    seen = set()
    for claim in claims:
        if claim["claim_no"] not in seen:
            unique_claims.append(claim)
            seen.add(claim["claim_no"])

    return unique_claims


def split_oa_paragraphs(oa_text: str):
    text = oa_text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r'[ \t]+', ' ', text)
    text = text.strip()

    # PDF 추출 텍스트에서 주요 구분자 앞에 강제로 줄바꿈 삽입
    split_markers = [
        r'\[구체적인 거절이유\]',
        r'\[심사결과\]',
        r'거절이유가 있는 부분',
        r'특허 가능한 청구항',
        r'청구항\s*제?\d+항',
        r'인용발명\s*\d+',
        r'^\s*\d+\.',
    ]

    for marker in split_markers:
        text = re.sub(rf'(?<!\n)({marker})', r'\n\1', text, flags=re.MULTILINE)

    # 1차 분리
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n+|\n(?=\[)|\n(?=\d+\.)', text) if p.strip()]

    # 너무 긴 문단은 추가 분리
    refined = []
    for p in paragraphs:
        if len(p) > 500:
            parts = re.split(
                r'(?=(청구항\s*제?\d+항))|(?=(인용발명\s*\d+))|(?=(\[구체적인 거절이유\]))',
                p
            )
            merged = []
            buf = ""
            for part in parts:
                if not part:
                    continue
                if re.match(r'청구항\s*제?\d+항|인용발명\s*\d+|\[구체적인 거절이유\]', part):
                    if buf.strip():
                        merged.append(buf.strip())
                    buf = part
                else:
                    buf += " " + part
            if buf.strip():
                merged.append(buf.strip())
            refined.extend([x.strip() for x in merged if x.strip()])
        else:
            refined.append(p)

    return refined


def parse_claim_numbers(text: str):
    claim_numbers = set()

    normalized = re.sub(r'\s+', ' ', text)

    # "청구항 제1항, 제3항 내지 제5항, 제9항 내지 제14항, 제16항, 제17항"
    # 같은 덩어리를 먼저 잡음
    block_pattern = r'청구항\s*((?:제?\d+\s*항(?:\s*(?:,|및|내지)\s*제?\d+\s*항)*))'
    blocks = re.findall(block_pattern, normalized)

    for block in blocks:
        # 범위 먼저 처리: 제3항 내지 제5항
        for a, b in re.findall(r'제?\s*(\d+)\s*항\s*내지\s*제?\s*(\d+)\s*항', block):
            a, b = int(a), int(b)
            for n in range(min(a, b), max(a, b) + 1):
                claim_numbers.add(n)

        # 개별 항 처리: 제1항, 제16항 등
        for n in re.findall(r'제?\s*(\d+)\s*항', block):
            claim_numbers.add(int(n))

    return sorted(claim_numbers)


def classify_rejection(text: str):
    normalized = re.sub(r'\s+', ' ', text)

    if (
        "제29조 제2항" in normalized
        or "제29조제2항" in normalized
        or "29조 2항" in normalized
        or "29조제2항" in normalized
        or "용이하게 발명" in normalized
        or "통상의 기술자가 용이하게" in normalized
        or "쉽게 발명할 수 있는 것" in normalized
        or "진보성" in normalized
    ):
        return "inventive_step", "진보성"

    if (
        "제29조 제1항" in normalized
        or "제29조제1항" in normalized
        or "29조 1항" in normalized
        or "29조제1항" in normalized
        or "신규성" in normalized
        or "동일" in normalized
    ):
        return "novelty", "신규성"

    if (
        "제42조" in normalized
        or "명확하지" in normalized
        or "불명확" in normalized
        or "간결하지" in normalized
        or "뒷받침" in normalized
        or "기재되어 있지" in normalized
        or "기재불비" in normalized
        or "명확성" in normalized
    ):
        return "clarity", "명확성"

    return "other", "기타"


def extract_rejections_from_oa(oa_text: str):
    paragraphs = split_oa_paragraphs(oa_text)
    results = []

    print("\n[디버그] OA 문단 수:", len(paragraphs))

    for idx, para in enumerate(paragraphs, start=1):
        claim_numbers = parse_claim_numbers(para)
        rejection_type, label = classify_rejection(para)

        print(f"\n[디버그] 문단 {idx}")
        print(para[:300])
        print("-> 추출 claim_numbers:", claim_numbers)
        print("-> 분류:", rejection_type, label)

        if not claim_numbers:
            continue

        results.append({
            "claim_numbers": claim_numbers,
            "type": rejection_type,
            "label": label,
            "oa_text": para
        })

    return results


def collect_claim_passages(oa_text: str, window_size: int = 3):
    paragraphs = split_oa_paragraphs(oa_text)
    passages_map = defaultdict(list)

    for idx, paragraph in enumerate(paragraphs):
        claim_numbers = parse_claim_numbers(paragraph)
        if not claim_numbers:
            continue

        window = paragraphs[idx:idx + window_size + 1]
        for claim_no in claim_numbers:
            for item in window:
                compact = normalize_for_match(item)
                if len(compact) < 8:
                    continue
                if item not in passages_map[claim_no]:
                    passages_map[claim_no].append(item)

    return passages_map


def attach_rejections_to_claims(claims, rejections):
    claim_map = {c["claim_no"]: c for c in claims}

    for rej in rejections:
        for claim_no in rej["claim_numbers"]:
            if claim_no not in claim_map:
                continue

            existing_types = [r["type"] for r in claim_map[claim_no]["rejections"]]

            display_message = make_display_message(
                rej["type"],
                rej["oa_text"],
                existing_types_for_claim=existing_types
            )

            if display_message is None:
                continue

            # 같은 청구항 안에서만 동일 메시지 중복 제거
            already_exists = any(
                r.get("display_message") == display_message and r.get("type") == rej["type"]
                for r in claim_map[claim_no]["rejections"]
            )
            if already_exists:
                continue

            claim_map[claim_no]["rejections"].append({
                "type": rej["type"],
                "label": rej["label"],
                "display_message": display_message,
                "oa_text": rej["oa_text"]
            })

    return claims


def normalize_for_match(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", text).lower()


def extract_match_tokens(text: str):
    return [
        token.lower()
        for token in re.findall(r"[0-9A-Za-z가-힣]+", text)
        if len(token) >= 2
    ]


def extract_claim_fragments(claim_text: str):
    fragments = []
    seen = set()

    primary_parts = re.split(r"[\n,;:]+", claim_text)
    secondary_delimiters = [
        r"\s+및\s+",
        r"\s+또는\s+",
        r"\s+그리고\s+",
        r"\s+이며\s+",
        r"\s+이고\s+",
    ]

    for part in primary_parts:
        nested_parts = [part]
        for delimiter in secondary_delimiters:
            expanded = []
            for item in nested_parts:
                expanded.extend(re.split(delimiter, item))
            nested_parts = expanded

        for item in nested_parts:
            fragment = item.strip(" .()[]{}")
            compact = normalize_for_match(fragment)
            if len(compact) < 6:
                continue
            if compact in seen:
                continue
            seen.add(compact)
            fragments.append(fragment)

    fragments.sort(key=len, reverse=True)
    return fragments


def score_fragment_against_rejection(fragment: str, oa_text: str):
    fragment_compact = normalize_for_match(fragment)
    oa_compact = normalize_for_match(oa_text)

    if fragment_compact and fragment_compact in oa_compact:
        return 10.0

    fragment_tokens = extract_match_tokens(fragment)
    if len(fragment_tokens) < 2:
        return 0.0

    oa_tokens = set(extract_match_tokens(oa_text))
    overlap = [token for token in fragment_tokens if token in oa_tokens]
    if len(overlap) < 2:
        return 0.0

    return len(set(overlap)) / len(set(fragment_tokens))


def build_inline_annotations(claim_text: str, rejections, oa_passages=None):
    fragments = extract_claim_fragments(claim_text)
    annotations = {}
    reference_texts = [rejection["oa_text"] for rejection in rejections]
    if oa_passages:
        reference_texts.extend(oa_passages)

    for rejection in rejections:
        scored = []
        for fragment in fragments:
            score = max(
                [score_fragment_against_rejection(fragment, text) for text in reference_texts],
                default=0.0
            )
            if score >= 0.55:
                scored.append((score, fragment))

        scored.sort(key=lambda item: (-item[0], -len(item[1])))

        for _, fragment in scored[:2]:
            key = normalize_for_match(fragment)
            if key not in annotations:
                annotations[key] = {
                    "text": fragment,
                    "types": [],
                    "labels": [],
                    "messages": []
                }

            if rejection["type"] not in annotations[key]["types"]:
                annotations[key]["types"].append(rejection["type"])
            if rejection["label"] not in annotations[key]["labels"]:
                annotations[key]["labels"].append(rejection["label"])
            if rejection["display_message"] not in annotations[key]["messages"]:
                annotations[key]["messages"].append(rejection["display_message"])

    return list(annotations.values())


def render_claim_text_with_highlights(claim_text: str, annotations):
    if not annotations:
        return html.escape(claim_text)

    matches = []
    for annotation in annotations:
        pattern = re.escape(annotation["text"])
        for match in re.finditer(pattern, claim_text):
            matches.append({
                "start": match.start(),
                "end": match.end(),
                "annotation": annotation
            })

    if not matches:
        return html.escape(claim_text)

    matches.sort(key=lambda item: (item["start"], -(item["end"] - item["start"])))

    selected = []
    cursor = -1
    for match in matches:
        if match["start"] < cursor:
            continue
        selected.append(match)
        cursor = match["end"]

    parts = []
    last = 0
    for match in selected:
        parts.append(html.escape(claim_text[last:match["start"]]))

        annotation = match["annotation"]
        badge_html = "".join(
            f'<span class="inline-badge inline-badge-{html.escape(rejection_type)}">{html.escape(label)}</span>'
            for rejection_type, label in zip(annotation["types"], annotation["labels"])
        )
        tooltip = " / ".join(annotation["messages"])
        primary_type = annotation["types"][0] if annotation["types"] else "other"

        parts.append(
            f'<mark class="claim-highlight claim-highlight-{html.escape(primary_type)}" '
            f'title="{html.escape(tooltip)}">{html.escape(claim_text[match["start"]:match["end"]])}'
            f'{badge_html}</mark>'
        )
        last = match["end"]

    parts.append(html.escape(claim_text[last:]))
    return "".join(parts)


def enrich_claims_for_display(claims):
    for claim in claims:
        annotations = build_inline_annotations(
            claim["text"],
            claim.get("rejections", []),
            claim.get("oa_passages", [])
        )
        claim["inline_annotations"] = annotations
        claim["inline_text_html"] = render_claim_text_with_highlights(claim["text"], annotations)

    return claims


def save_result_json(claims, rejections):
    result = {
        "claims": claims,
        "oa_rejections": rejections
    }
    out_path = OUTPUT_DIR / "result.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    oa_file = request.files.get("oa_file")
    claims_file = request.files.get("claims_file")

    if not oa_file or not claims_file:
        flash("OA 파일과 청구항 파일을 모두 업로드하세요.")
        return redirect(url_for("index"))

    if oa_file.filename == "" or claims_file.filename == "":
        flash("파일이 선택되지 않았습니다.")
        return redirect(url_for("index"))

    try:
        oa_path = UPLOAD_DIR / oa_file.filename
        claims_path = UPLOAD_DIR / claims_file.filename

        oa_file.save(oa_path)
        claims_file.save(claims_path)

        oa_text = read_uploaded_file_text(oa_path)
        claims_text = read_uploaded_file_text(claims_path)

        claims = split_claims(claims_text)
        claims = sorted(claims, key=lambda x: x["claim_no"])

        rejections = extract_rejections_from_oa(oa_text)
        claims = attach_rejections_to_claims(claims, rejections)
        claim_passages = collect_claim_passages(oa_text)
        for claim in claims:
            claim["oa_passages"] = claim_passages.get(claim["claim_no"], [])
        claims = enrich_claims_for_display(claims)

        display_rejection_count = count_display_rejections(claims)

        save_result_json(claims, rejections)

        return render_template(
            "result.html",
            claims=claims,
            rejections=rejections,
            oa_filename=oa_file.filename,
            claims_filename=claims_file.filename,
            display_rejection_count=display_rejection_count
        )

    except Exception as e:
        flash(f"오류가 발생했습니다: {str(e)}")
        return redirect(url_for("index"))
    
    
def count_display_rejections(claims):
    """
    거절이유가 하나라도 붙은 청구항 수를 센다.
    예: 1,3,4,5,9~14,16,17이면 12
    """
    count = 0
    for claim in claims:
        visible_rejections = [
            rej for rej in claim.get("rejections", [])
            if rej.get("type") != "other"
        ]
        if visible_rejections:
            count += 1
    return count

def extract_cited_inventions(text: str):
    """
    '인용발명 1', '인용발명 2, 3', '인용발명 1 내지 4' 등을 간단히 추출
    """
    cited = set()

    normalized = re.sub(r'\s+', ' ', text)

    # 범위: 인용발명 1 내지 4
    for a, b in re.findall(r'인용발명\s*(\d+)\s*내지\s*(\d+)', normalized):
        a, b = int(a), int(b)
        for n in range(min(a, b), max(a, b) + 1):
            cited.add(n)

    # 개별: 인용발명 1, 인용발명 2
    for n in re.findall(r'인용발명\s*(\d+)', normalized):
        cited.add(int(n))

    return sorted(cited)


def make_display_message(rejection_type: str, oa_text: str, existing_types_for_claim=None):
    """
    청구항 표시용 짧은 메시지 생성
    """
    if rejection_type == "other":
        return None

    if existing_types_for_claim is None:
        existing_types_for_claim = []

    if rejection_type == "inventive_step":
        inventive_count = existing_types_for_claim.count("inventive_step")

        # 첫 번째 진보성
        if inventive_count == 0:
            return "진보성 거절이유 해당됩니다."

        # 두 번째 이후 진보성
        cited = extract_cited_inventions(oa_text)
        if cited:
            cited_str = ",".join(str(x) for x in cited)
            return f"인용발명 {cited_str}와 구성요소 비교"
        return "인용발명과 구성요소 비교"

    if rejection_type == "novelty":
        return "신규성 거절이유 해당됩니다."

    if rejection_type == "clarity":
        return "명확성 거절이유 해당됩니다."

    return None

if __name__ == "__main__":
    app.run(debug=True)
