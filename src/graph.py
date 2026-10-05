"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)

Ontology actually built here (default; KG_ONTOLOGY=hint switches back to the suggested one above).
Design notes: report/ONTOLOGY.md.

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, severity, text})
    (:Clause)-[:THRESHOLD {point, min_g, max_g, text}]->(:Substance {name, aliases})
    (:Case {id = doc_id#i, name, summary, stage, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount, amount_g}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {key, name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import math
import os
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    target = normalize(name or "")
    if not target:
        return None
    by_normalized: dict[str, str] = {}
    for original in known:
        by_normalized.setdefault(normalize(original), original)
    if target in by_normalized:
        return by_normalized[target]
    close = difflib.get_close_matches(target, list(by_normalized), n=1, cutoff=0.8)
    return by_normalized[close[0]] if close else None

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
    return cases

# ----------------------------------------------------------------------------------------------
# Own ontology (default) — see report/ONTOLOGY.md. KG_ONTOLOGY=hint switches back to the HINT ontology.
#
#   (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
#   (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, severity, text, doc_id})
#   (:Clause)-[:THRESHOLD {point, min_g, max_g, text}]->(:Substance {name, aliases})
#   (:Case {id = doc_id#i, name, summary, stage, date, doc_id})-[:CHARGED_WITH]->(:Crime)
#   (:Case)-[:INVOLVES {amount, amount_g}]->(:Substance)
#   (:Case)-[:LOCATED_IN]->(:Location {name})
#   (:Person {key, name, aliases})-[:INVOLVED_IN {role, charge, sentence}]->(:Case)
# ----------------------------------------------------------------------------------------------

LEGAL_QUESTION = re.compile(r"\b(điều|tội|khoản|khung|phạt|luật|tù|hình sự)\b", re.IGNORECASE)

def _bigrams(text: str) -> set[tuple[str, str]]:
    words = re.findall(r"\w+", text.lower())
    return set(zip(words, words[1:]))

def ontology() -> str:
    """'own' (default) or 'hint'. Read at call time so .env / the shell can switch it per run."""
    return os.getenv("KG_ONTOLOGY", "own").strip().lower()

# Street names and spelling variants seen in Vietnamese news -> canonical names from the law KB.
SUBSTANCE_ALIASES = {
    "Heroine": ["heroin", "hêrôin", "hê rô in"],
    "Cocaine": ["cocain", "côcain"],
    "Methamphetamine": ["ma túy đá", "ma tuý đá", "methamphetamin", "meth", "hồng phiến"],
    "Amphetamine": ["amphetamin"],
    "MDMA": ["thuốc lắc", "ecstasy"],
    "Ketamine": ["ketamin", "ketamine (ke)"],
    "cần sa": ["cannabis", "marijuana", "bồ đà", "cây cần sa", "nhựa cần sa"],
    "thuốc phiện": ["opium", "nhựa thuốc phiện"],
    "côca": ["coca", "lá côca", "lá coca"],
}
_SUBSTANCE_LOOKUP = {alias: canon for canon, aliases in SUBSTANCE_ALIASES.items() for alias in [canon, *aliases]}

def _clean(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    return re.sub(r"\s+", " ", text.strip().strip("\"'“”‘’").lower())

def normalize_substance(name: str) -> str:
    return _clean(name).removeprefix("chất ").removeprefix("ma túy ").removeprefix("ma tuý ").strip() or _clean(name)

def canonical_substance(name: str) -> str:
    """'thuốc lắc (MDMA)' -> 'MDMA'; unknown names are kept, lower-cased, so they still merge with each other."""
    for part in [name, *re.split(r"[()/,;]| hoặc | và ", name or "")]:
        hit = link_entity(part, list(_SUBSTANCE_LOOKUP), normalize_substance)
        if hit:
            return _SUBSTANCE_LOOKUP[hit]
    return _clean(name)

PERSON_PREFIXES = ("bị cáo ", "bị can ", "nghi phạm ", "đối tượng ", "ông ", "bà ", "anh ", "chị ", "giang hồ ")

def normalize_person(name: str) -> str:
    name = _clean(name)
    for prefix in PERSON_PREFIXES:
        name = name.removeprefix(prefix)
    return name.strip()

def normalize_location(name: str) -> str:
    name = _clean(name).replace("tp. ", "tp.").replace("tp ", "tp.")
    for prefix in ("thành phố ", "tp.", "tỉnh "):
        name = name.removeprefix(prefix)
    return {"hcm": "hồ chí minh", "sài gòn": "hồ chí minh"}.get(name, name).strip()

UNIT_GRAMS = {"kilôgam": 1000, "kilogam": 1000, "kilogram": 1000, "kg": 1000,
              "gam": 1, "gram": 1, "g": 1, "miligam": 0.001, "mg": 0.001}
AMOUNT = re.compile(r"(\d+(?:[.,]\d+)*)\s*(kilôgam|kilogam|kilogram|kg|miligam|gam|gram|mg|g)(?![a-zà-ỹ])", re.IGNORECASE)

def _number(text: str) -> float:
    # Vietnamese writes 9,6 for 9.6 and 1.200 for 1200.
    if "," in text or re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        text = text.replace(".", "").replace(",", ".")
    return float(text)

def amount_in_grams(text: str) -> float | None:
    """'hơn 9,6kg' -> 9600.0, '406g' -> 406.0, 'không rõ' -> None."""
    match = AMOUNT.search(text or "")
    return _number(match.group(1)) * UNIT_GRAMS[match.group(2).lower()] if match else None

POINT_START = re.compile(r"^([a-zđ])\)\s*(.+)$", re.MULTILINE)
RANGE = re.compile(r"từ (\d+(?:,\d+)?) (gam|kilôgam) đến dưới (\d+(?:,\d+)?) (gam|kilôgam)")
AT_LEAST = re.compile(r"(\d+(?:,\d+)?) (gam|kilôgam) trở lên")

def parse_thresholds(clause_text: str) -> list[dict]:
    """One row per (point, substance): the weight band [min_g, max_g) that puts a case into this clause."""
    rows, seen = [], set()
    for point, text in POINT_START.findall(clause_text):
        min_g = max_g = None
        if m := RANGE.search(text):
            min_g = _number(m.group(1)) * UNIT_GRAMS[m.group(2)]
            max_g = _number(m.group(3)) * UNIT_GRAMS[m.group(4)]
        elif m := AT_LEAST.search(text):
            min_g = _number(m.group(1)) * UNIT_GRAMS[m.group(2)]
        for substance in find_substances(text):
            rows.append({"substance": substance, "point": point, "min_g": min_g, "max_g": max_g, "text": text})
            seen.add(substance)
    for substance in find_substances(clause_text):   # substance named outside any point
        if substance not in seen:
            rows.append({"substance": substance, "point": "", "min_g": None, "max_g": None, "text": ""})
    return rows

def penalty_severity(penalty: str) -> int:
    """Rank a clause's penalty: max years of prison, chung thân = 30, tử hình = 40, no prison = 0."""
    if "tù" not in penalty and "tử hình" not in penalty:
        return 0
    if "tử hình" in penalty:
        return 40
    if "chung thân" in penalty:
        return 30
    return max((int(y) for y in re.findall(r"(\d+) năm", penalty)), default=0)

def parse_law_article_own(doc: Document) -> dict[str, Any]:
    article = parse_law_article(doc)
    for clause in article["clauses"]:
        clause["severity"] = penalty_severity(clause["penalty"])
        clause["thresholds"] = parse_thresholds(clause["text"])
    return article

OWN_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Bỏ qua các tin liên quan/đọc thêm chèn giữa bài (nói về vụ khác).
Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "stage": "giai đoạn mới nhất trong bài: bắt giữ|khởi tố|truy tố|xét xử sơ thẩm|xét xử phúc thẩm|khác",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, chọn nguyên văn từ DANH SÁCH TỘI DANH; vụ mới bị bắt chưa khởi tố thì chọn tội ứng với hành vi bị điều tra"],
  "substances": [{{"name": "MỌI chất ma túy của vụ, kể cả chất nêu trong kết luận giám định; dùng tên chuẩn trong DANH SÁCH CHẤT (thuốc lắc, ma túy 'kẹo' = MDMA; ma túy đá = Methamphetamine; ketamin = Ketamine; heroin = Heroine)",
                   "amount": "khối lượng/số lượng kèm đơn vị như trong bài (ví dụ: hơn 9,6kg; 5 viên), chuỗi rỗng nếu không có"}}],
  "people": [{{"name": "họ tên đầy đủ (không kèm chức danh như bị cáo/ông/bà)", "aliases": ["biệt danh, tên mạng"],
               "role": "bị cáo|bị can|nghi phạm|người liên quan",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 36 tháng tù; chuỗi rỗng nếu chưa xét xử"}}]
}}]}}
Không đưa cán bộ, công an, luật sư, phóng viên vào "people"; không bịa tên (bỏ qua người không có họ tên hay biệt danh cụ thể).
Trường không có thông tin thì để "" (chuỗi rỗng thật, không viết chữ "chuỗi rỗng").
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

class PersonRegistry:
    """Resolve the same person across articles by full name OR any alias ('Hoàng Nato' = 'Dương Minh Tuấn')."""

    def __init__(self) -> None:
        self.key_of: dict[str, str] = {}
        self.aliases: dict[str, list[str]] = {}

    def resolve(self, name: str, aliases: list[str]) -> tuple[str, list[str]]:
        mentions = [m for m in [name, *aliases] if normalize_person(m)]
        normalized = [normalize_person(m) for m in mentions]
        key = next((self.key_of[n] for n in normalized if n in self.key_of), normalized[0])
        for n in normalized:
            self.key_of.setdefault(n, key)
        known = self.aliases.setdefault(key, [])
        known += [m for m in mentions if m not in known]
        return key, list(known)

PLACEHOLDERS = {"chuỗi rỗng", "không rõ", "không có", "chưa rõ", "n/a", "none", "null"}

def _blank_placeholders(value: Any) -> Any:
    """gpt-4o-mini sometimes writes the instruction ('chuỗi rỗng') instead of an empty string."""
    if isinstance(value, dict):
        for k, v in value.items():
            value[k] = _blank_placeholders(v)
    elif isinstance(value, list):
        return [_blank_placeholders(v) for v in value]
    elif isinstance(value, str) and value.strip().lower() in PLACEHOLDERS:
        return ""
    return value

def extract_news_cases_own(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    prompt = OWN_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        _blank_placeholders(case)
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        case["people"] = [p for p in case.get("people", []) if not re.search(r"\d", p.get("name") or "")]
        merged: dict[str, dict] = {}
        for s in case.get("substances", []):
            if not s.get("name"):
                continue
            name = canonical_substance(s["name"])
            amount = s.get("amount") or ""
            grams = amount_in_grams(amount)
            if name not in merged or (grams or 0) > (merged[name]["amount_g"] or 0):
                merged[name] = {"name": name, "amount": amount, "amount_g": grams}
        case["substances"] = list(merged.values())
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
    return cases

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "name"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- own ontology: writes

    def own_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "id"),
                           ("Substance", "name"), ("Person", "key"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")
        self.run("UNWIND $rows AS row MERGE (s:Substance {name: row.name}) SET s.aliases = row.aliases",
                 rows=[{"name": k, "aliases": v} for k, v in SUBSTANCE_ALIASES.items()])

    def add_law_article_own(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.severity = clause.severity,
                  cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (t IN clause.thresholds |
                MERGE (s:Substance {name: t.substance})
                MERGE (cl)-[r:THRESHOLD {point: t.point}]->(s)
                SET r.min_g = t.min_g, r.max_g = t.max_g, r.text = t.text)
            """,
            **article,
        )

    def add_news_case_own(self, case: dict, case_id: str, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {id: $case_id})
              SET k.name = $name, k.summary = $summary, k.stage = $stage, k.date = $date,
                  k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount, r.amount_g = s.amount_g)
            FOREACH (p IN $people | MERGE (person:Person {key: p.key})
                ON CREATE SET person.name = p.name
                SET person.aliases = p.aliases
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            case_id=case_id, name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), stage=case.get("stage", ""), date=case.get("date", ""),
            location=case.get("location", ""), charges=case.get("charges", []),
            substances=case.get("substances", []), people=case.get("people", []),
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Graph facts for a question: seeds + 1 hop, then the legal basis of every case reached."""
        if ontology() == "hint":
            return self.context_hint(question, doc_ids, max_facts)
        seed_ids, seed_facts = self.seed_facts(question, doc_ids, skip_labels=("Clause",))

        # 1. Cases: from the retrieved articles, from people named in the question, and — only when no
        #    person is named — every case involving a substance named in the question (aggregation).
        cases = self.run(
            """
            WITH $ids AS ids
            WITH ids, EXISTS { MATCH (p:Person) WHERE elementId(p) IN ids } AS person_named
            MATCH (k:Case)
            WHERE elementId(k) IN ids
               OR EXISTS { MATCH (p:Person)-[:INVOLVED_IN]->(k) WHERE elementId(p) IN ids }
               OR (NOT person_named AND EXISTS { MATCH (k)-[:INVOLVES]->(s:Substance) WHERE elementId(s) IN ids })
            OPTIONAL MATCH (p:Person)-[r:INVOLVED_IN]->(k)
            WITH k, collect(p.name + CASE WHEN r.sentence <> '' THEN ' (' + r.role + ', ' + r.sentence + ')'
                                       ELSE ' (' + r.role + ')' END) AS people
            OPTIONAL MATCH (k)-[i:INVOLVES]->(s:Substance)
            WITH k, people, collect(s.name + CASE WHEN i.amount <> '' THEN ' ' + i.amount ELSE '' END) AS subs
            OPTIONAL MATCH (k)-[:CHARGED_WITH]->(c:Crime)
            RETURN elementId(k) AS id, k.name AS name, k.summary AS summary, k.stage AS stage,
                   k.doc_id AS doc_id, people, subs, collect(c.name) AS crimes
            ORDER BY k.doc_id IN $doc_ids DESC LIMIT 10
            """,
            ids=seed_ids, doc_ids=doc_ids,
        )
        case_facts = [
            f"Vụ việc '{k['name']}' [{k['stage'] or '?'}; nguồn {k['doc_id']}]: {k['summary']}"
            + (f" Tội danh: {', '.join(k['crimes'])}." if k["crimes"] else " Tội danh: chưa nối được với luật.")
            + (f" Chất: {', '.join(k['subs'])}." if k["subs"] else "")
            + (f" Người: {'; '.join(k['people'])}." if k["people"] else "")
            for k in cases
        ]

        # 2. Bridge: Case -> Crime <- Article -> Clause, only when the question asks something legal. Keep the
        #    base clause, the most severe clause, and the clause whose weight band [min_g, max_g) holds the amount.
        rows = []
        if LEGAL_QUESTION.search(question):
            rows = self.run(
                """
                MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
                WHERE elementId(k) IN $case_ids
                CALL (a) { MATCH (a)-[:HAS_CLAUSE]->(x:Clause) RETURN max(x.severity) AS top }
                OPTIONAL MATCH (k)-[i:INVOLVES]->(s:Substance)<-[t:THRESHOLD]-(cl)
                WITH k, a, cl, top, collect(CASE WHEN s IS NULL THEN NULL ELSE
                     {substance: s.name, amount: i.amount, amount_g: i.amount_g,
                      min_g: t.min_g, max_g: t.max_g, point: t.point, text: t.text} END) AS hits
                RETURN k.name AS case_name, a.id AS article, a.title AS title, cl.number AS number, cl.text AS text,
                       cl.severity AS severity, top, hits
                ORDER BY article, number
                """,
                case_ids=[k["id"] for k in cases],
            )
        # 3. Articles named in the question: same selection, using the substances/amount in the question.
        numbers = re.findall(r"[Đđ]iều (\d+)", question)
        if numbers:
            asked = [{"substance": s, "amount": "", "amount_g": amount_in_grams(question)}
                     for s in find_substances(question)]
            rows += [dict(r, hits=[dict(t, **q) for t in r["hits"] for q in asked if q["substance"] == t["substance"]])
                     for r in self.run(
                """
                MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause) WHERE any(n IN $numbers WHERE a.id STARTS WITH 'Điều ' + n + ' ')
                CALL (a) { MATCH (a)-[:HAS_CLAUSE]->(x:Clause) RETURN max(x.severity) AS top }
                OPTIONAL MATCH (cl)-[t:THRESHOLD]->(s:Substance)
                WITH a, cl, top, collect(CASE WHEN s IS NULL THEN NULL ELSE
                     {substance: s.name, min_g: t.min_g, max_g: t.max_g, point: t.point, text: t.text} END) AS hits
                RETURN 'nêu trong câu hỏi' AS case_name, a.id AS article, a.title AS title, cl.number AS number,
                       cl.text AS text, cl.severity AS severity, top, hits
                ORDER BY article, number
                """,
                numbers=numbers)]

        selected: dict[tuple, dict] = {}   # one fact per clause, listing every case that reaches it
        for r in rows:
            reasons, points = [], []
            if r["number"] == 1:
                reasons.append("khung cơ bản")
            if r["top"] and r["severity"] == r["top"]:
                reasons.append("khung cao nhất")
            for h in r["hits"]:   # an unknown amount (None) cannot pick a band, so it adds nothing
                if h["amount_g"] is not None and h["min_g"] is not None \
                        and h["min_g"] <= h["amount_g"] < (h["max_g"] or float("inf")):
                    reasons.append(f"{h['substance']} {h['amount'] or ''} thuộc mức điểm {h['point']}".replace("  ", " "))
                    points.append(f"điểm {h['point']}) {h['text']}")
            if reasons:
                fact = selected.setdefault((r["article"], r["number"]), {
                    "row": r, "reasons": [], "points": [], "cases": []})
                fact["reasons"] += reasons
                fact["points"] += points
                fact["cases"].append(r["case_name"])
        law_facts = [
            f"[{r['article']} - {r['title']}] khoản {r['number']} ({'; '.join(dict.fromkeys(f['reasons']))}; "
            f"vụ: {', '.join(dict.fromkeys(f['cases']))}): {r['text'].splitlines()[0]}"
            + "".join(f" | {p}" for p in dict.fromkeys(f["points"]))
            for f in selected.values() for r in [f["row"]]
        ]

        # 4. Pure law question (no case reached): hop Article -> Clause by word overlap with the question.
        clause_facts = [] if cases else self.matching_clauses(question, doc_ids)
        facts = list(dict.fromkeys(case_facts + law_facts + clause_facts + seed_facts))
        return facts[:max_facts]

    def matching_clauses(self, question: str, doc_ids: list[str], k: int = 2, min_score: float = 3.0) -> list[str]:
        """Article -> Clause hop for retrieved law articles: the clauses sharing the rarest word pairs with the
        question (e.g. 'tiền chất' -> Điều 2 Luật PCMT khoản 'Tiền chất là ...')."""
        clauses = self.run("""
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            RETURN a.id AS article, a.title AS title, a.doc_id AS doc_id, cl.number AS number, cl.text AS text""")
        asked = _bigrams(question)
        df = Counter(g for c in clauses for g in _bigrams(c["text"]) & asked)
        scored = sorted(((sum(math.log(len(clauses) / df[g]) for g in _bigrams(c["text"]) & asked), c)
                         for c in clauses if c["doc_id"] in doc_ids), key=lambda x: -x[0])
        return [f"[{c['article']} - {c['title']}] khoản {c['number']}: {c['text'][:800]}"
                for score, c in scored[:k] if score >= min_score]

    def context_hint(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """KG-3 over the HINT ontology (kept for the before/after comparison in report/ONTOLOGY.md §7)."""
        seed_ids, facts = self.seed_facts(question, doc_ids)
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
            RETURN elementId(k) AS id, k.name AS name, k.summary AS summary
            """,
            ids=seed_ids,
        )
        law_facts = [f"Vụ việc '{k['name']}': {k['summary']}" for k in cases]
        rows = self.run(
            """
            MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE elementId(k) IN $case_ids
              AND (cl.number = 1 OR EXISTS { (k)-[:INVOLVES]->(:Substance)<-[:MENTIONS]-(cl) })
            RETURN DISTINCT a.id AS article, a.title AS title, cl.number AS number, cl.text AS text
            UNION
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE any(n IN $numbers WHERE a.id STARTS WITH 'Điều ' + n + ' ')
              AND (cl.number = 1 OR EXISTS { MATCH (cl)-[:MENTIONS]->(s:Substance) WHERE s.name IN $subs })
            RETURN DISTINCT a.id AS article, a.title AS title, cl.number AS number, cl.text AS text
            """,
            case_ids=[k["id"] for k in cases], numbers=re.findall(r"[Đđ]iều (\d+)", question),
            subs=find_substances(question),
        )
        law_facts += [f"[{r['article']} - {r['title']}] khoản {r['number']}: {r['text']}" for r in rows]
        return list(dict.fromkeys(law_facts + facts))[:max_facts]

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    json_llm = lambda prompt: llm_fn(prompt, json_mode=True)  # noqa: E731
    if ontology() == "hint":
        graph.suggested_constraints()
        articles = [parse_law_article(d) for d in law_docs]
        for article in articles:
            graph.add_law_article(article)
        crimes = [a["crime"] for a in articles if a["crime"]]
        for doc in news_docs:
            for case in extract_news_cases(doc, json_llm, crimes):
                graph.add_news_case(case, doc)
        return

    graph.own_constraints()
    articles = [parse_law_article_own(d) for d in law_docs]        # regex: deterministic, free
    for article in articles:
        graph.add_law_article_own(article)
    crimes = [a["crime"] for a in articles if a["crime"]]
    people = PersonRegistry()
    locations: dict[str, str] = {}
    for doc in news_docs:                                          # LLM: one call per article
        for index, case in enumerate(extract_news_cases_own(doc, json_llm, crimes)):
            location = case.get("location") or ""
            case["location"] = locations.setdefault(normalize_location(location), location) if location else ""
            resolved = []
            for p in case.get("people", []):
                if not normalize_person(p.get("name") or ""):
                    continue
                key, aliases = people.resolve(p["name"], [a for a in p.get("aliases") or [] if a])
                resolved.append({"key": key, "name": p["name"], "aliases": aliases, "role": p.get("role") or "",
                                 "charge": p.get("charge") or "", "sentence": p.get("sentence") or ""})
            case["people"] = resolved
            graph.add_news_case_own(case, f"{doc.id}#{index}", doc)

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids = list(dict.fromkeys(
            chunk["metadata"].get("doc_id") or chunk["id"].split("#")[0] for chunk in chunks))
        facts = self.graph.context(question, doc_ids)
        prompt = GRAPH_PROMPT.format(
            facts="\n".join(f"- {fact}" for fact in facts) or "- (không có)",
            chunks="\n\n".join(f"[{i}] {chunk['content']}" for i, chunk in enumerate(chunks, start=1)),
            question=question,
        )
        return self.llm_fn(prompt)
