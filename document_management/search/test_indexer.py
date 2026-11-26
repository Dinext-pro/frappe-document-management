import json
import sys
from pathlib import Path
from types import SimpleNamespace

from document_management.search import tantivy_backend
from document_management.search.indexer import (
    _parse_doctypes,
    get_document_content,
    get_document_comments,
    get_document_title,
    get_indexable_content,
    handle_doc_save,
    handle_doc_trash,
)
from document_management.search.query import (
    build_natural_query,
    fold_text,
    make_excerpt,
    normalize_language,
    query_tokens,
    search_match_details,
    significant_terms,
)


class Row(dict):
    def __init__(self, fields, **values):
        super().__init__(values)
        self.meta = SimpleNamespace(fields=fields)

    def get(self, key, default=None):
        return super().get(key, default)


def _field(fieldname, fieldtype, label=None, **values):
    return SimpleNamespace(
        fieldname=fieldname,
        fieldtype=fieldtype,
        label=label or fieldname,
        **values,
    )


def test_generic_index_includes_links_dates_amounts_and_children():
    child = Row(
        [
            _field("account", "Link", "Account"),
            _field("amount", "Currency", "Amount"),
        ],
        account="Cash - CO",
        amount=1250.50,
    )
    document = Row(
        [
            _field("customer", "Link", "Customer"),
            _field("status", "Select", "Status"),
            _field("posting_date", "Date", "Posting Date"),
            _field("items", "Table", "Items"),
        ],
        customer="CUS-001",
        status="Active",
        posting_date="2026-06-13",
        items=[child],
    )

    content = get_document_content(document)

    assert "Customer: CUS-001" in content
    assert "Status: Active" in content
    assert "Posting Date: 2026-06-13" in content
    assert "Account: Cash - CO" in content
    assert "Amount: 1250.5" in content


def test_generic_index_excludes_hidden_and_restricted_fields():
    document = Row(
        [
            _field("public_note", "Data", "Public Note"),
            _field("hidden_note", "Data", "Hidden Note", hidden=1),
            _field("restricted_note", "Data", "Restricted Note", permlevel=1),
        ],
        public_note="Visible",
        hidden_note="Secret hidden value",
        restricted_note="Secret restricted value",
    )

    content = get_document_content(document)

    assert "Public Note: Visible" in content
    assert "Secret hidden value" not in content
    assert "Secret restricted value" not in content


def test_generic_index_strips_html_from_rich_text():
    document = Row(
        [_field("description", "Text Editor", "Description")],
        description="<p>Approved <strong>contract</strong>&nbsp;today</p>",
    )

    content = get_document_content(document)

    assert content == "Description: Approved contract today"
    assert "<strong>" not in content


def test_comments_are_cleaned_and_added_to_index_content(monkeypatch):
    document = Row(
        [_field("subject", "Data", "Subject")],
        subject="Payroll setup",
    )
    document.doctype = "Task"
    document.name = "TASK-001"
    monkeypatch.setattr(
        "document_management.search.indexer.frappe.get_all",
        lambda *args, **kwargs: [
            {
                "reference_name": "TASK-001",
                "content": "<p>Review the <strong>payroll period</strong>.</p>",
            }
        ],
    )

    comments = get_document_comments("Task", ["TASK-001"])
    content = get_indexable_content(
        document,
        comments=comments["TASK-001"],
    )

    assert comments == {"TASK-001": ["Review the payroll period."]}
    assert "Subject: Payroll setup" in content
    assert "Comment: Review the payroll period." in content
    assert "<strong>" not in content


def test_comment_changes_reindex_the_parent_document(monkeypatch):
    settings = SimpleNamespace(
        enable_full_text_search=True,
        get=lambda key, default=None: (
            [SimpleNamespace(document_type="Task")]
            if key == "indexed_doctypes"
            else default
        ),
    )
    comment = Row(
        [],
        comment_type="Comment",
        reference_doctype="Task",
        reference_name="TASK-001",
    )
    comment.doctype = "Comment"
    comment.name = "COMMENT-001"
    queued = []
    monkeypatch.setattr(
        "document_management.search.indexer.frappe.get_single",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr(
        "document_management.search.indexer.frappe.enqueue",
        lambda method, **kwargs: queued.append((method, kwargs)),
    )

    handle_doc_save(comment)
    handle_doc_trash(comment)

    assert len(queued) == 2
    for method, kwargs in queued:
        assert method.endswith("run_indexing_background")
        assert kwargs["doc_type"] == "Task"
        assert kwargs["doc_name"] == "TASK-001"
        assert kwargs["enqueue_after_commit"] is True


def test_restricted_title_field_falls_back_to_document_name():
    title_field = _field(
        "private_title",
        "Data",
        "Private Title",
        permlevel=1,
    )
    document = Row([title_field], private_title="Confidential title")
    document.name = "REC-0001"
    document.meta.title_field = "private_title"
    document.meta.get_field = lambda fieldname: (
        title_field if fieldname == "private_title" else None
    )

    assert get_document_title(document) == "REC-0001"


def test_doctype_filter_accepts_json_and_rejects_invalid_shapes():
    assert _parse_doctypes('["Loan", "Payment"]') == {"Loan", "Payment"}
    assert _parse_doctypes('{"doctype": "Loan"}') == set()


def test_spanish_stopwords_are_not_search_criteria():
    assert query_tokens("Señal de alerta") == ["señal", "de", "alerta"]
    assert significant_terms("Señal de alerta", language="es-NI") == [
        "señal",
        "alerta",
    ]
    assert significant_terms(
        "¿Cuáles son las señales de alerta?",
        language="es",
    ) == [
        "señales",
        "alerta",
    ]
    assert significant_terms("de la", language="es") == []


def test_english_stopwords_are_not_search_criteria():
    assert significant_terms(
        "What are the warning signs of fraud?",
        language="en_US",
    ) == [
        "warning",
        "signs",
        "fraud",
    ]
    assert significant_terms(
        "the documents of the customer",
        language="en",
    ) == [
        "documents",
        "customer",
    ]


def test_french_stopwords_are_not_search_criteria():
    assert significant_terms(
        "Quels sont les signes d'alerte de fraude ?",
        language="fr-FR",
    ) == [
        "signes",
        "alerte",
        "fraude",
    ]
    assert significant_terms("les documents du client", language="fr") == [
        "documents",
        "client",
    ]


def test_only_selected_language_stopwords_are_applied():
    assert normalize_language("es-NI") == "es"
    assert normalize_language("en_US") == "en"
    assert normalize_language("Spanish") == "es"
    assert normalize_language("Español (Nicaragua)") == "es"
    assert normalize_language("French") == "fr"
    assert significant_terms("the documents", language="es") == [
        "the",
        "documents",
    ]


def test_excerpt_is_centered_on_a_matching_term():
    content = ("inicio " * 80) + "señal de alerta importante" + (" final" * 80)
    excerpt = make_excerpt(
        content,
        "señal de alerta",
        maximum=120,
        language="es",
    )

    assert "señal de alerta" in excerpt
    assert excerpt.startswith("...")
    assert excerpt.endswith("...")


def test_excerpt_matches_query_without_accents():
    content = ("inicio " * 80) + "Período de Nómina activo" + (
        " final" * 80
    )

    excerpt = make_excerpt(
        content,
        "periodo de nomina",
        maximum=120,
        language="es",
    )

    assert "Período de Nómina" in excerpt
    assert excerpt.startswith("...")


def test_long_document_excerpt_can_supply_list_card_context():
    content = ("contexto previo " * 80) + "riesgo de fraude" + (
        " contexto posterior" * 80
    )
    excerpt = make_excerpt(
        content,
        "riesgo de fraude",
        maximum=720,
        language="es",
    )

    assert "riesgo de fraude" in excerpt
    assert len(excerpt) <= 726
    assert len(excerpt) > 500


def test_fold_text_normalizes_accents_for_indexing():
    assert fold_text("Período de Nómina") == "periodo de nomina"


def test_search_match_details_prioritize_exact_accent_insensitive_title():
    details = search_match_details(
        "periodo de nomina",
        doc_name="PAYROLL-001",
        title="Período de Nómina",
        content="Configuración activa",
        language="es",
    )

    assert details["match_type"] == "exact"
    assert details["quality"] == 90
    assert details["coverage"] == 1


def test_search_match_details_report_partial_term_coverage():
    details = search_match_details(
        "contrato cliente vencido urgente",
        title="Contrato de cliente",
        content="Seguimiento pendiente",
        language="es",
    )

    assert details["match_type"] == "partial"
    assert details["matched_terms"] == 2
    assert details["total_terms"] == 4
    assert details["coverage"] == 0.5


def test_search_match_details_do_not_count_word_substrings():
    details = search_match_details(
        "pay contract",
        content="Payment contract",
        language="en",
    )

    assert details["matched_terms"] == 1
    assert details["coverage"] == 0.5


def test_natural_query_weights_fields_and_supports_relaxed_mode(monkeypatch):
    class FakeQuery:
        @staticmethod
        def empty_query():
            return ("empty",)

        @staticmethod
        def boost_query(query, weight):
            return ("boost", query, weight)

        @staticmethod
        def boolean_query(clauses):
            return ("boolean", clauses)

    fake_tantivy = SimpleNamespace(
        Query=FakeQuery,
        Occur=SimpleNamespace(Must="must", Should="should"),
    )
    monkeypatch.setitem(sys.modules, "tantivy", fake_tantivy)

    class FakeIndex:
        def parse_query(self, query, fields):
            return ("parsed", query, tuple(fields))

    strict = build_natural_query(
        FakeIndex(),
        "Nómina activa",
        {"doc_name": 6, "title": 4, "content": 1},
        language="es",
        require_all=True,
    )
    relaxed = build_natural_query(
        FakeIndex(),
        "Nómina activa",
        {"doc_name": 6, "title": 4, "content": 1},
        language="es",
        require_all=False,
    )

    strict_occurrences = [clause[0] for clause in strict[1][:2]]
    relaxed_occurrences = [clause[0] for clause in relaxed[1][:2]]
    assert strict_occurrences == ["must", "must"]
    assert relaxed_occurrences == ["should", "should"]
    assert "nomina" in repr(strict)
    assert "doc_name" in repr(strict)
    assert "6.0" in repr(strict)


def test_tantivy_search_prioritizes_exact_and_filters_weak_partial_hits(
    monkeypatch,
):
    class SearchResult:
        def __init__(self, hits, count):
            self.hits = hits
            self.count = count

    documents = {
        "exact": {
            "doc_type": ["Task"],
            "doc_name": ["TASK-EXACT"],
            "title": ["Contrato cliente vencido urgente"],
            "content": [""],
        },
        "generic": {
            "doc_type": ["Task"],
            "doc_name": ["TASK-GENERIC"],
            "title": ["Seguimiento"],
            "content": ["contrato cliente vencido urgente"],
        },
        "too-partial": {
            "doc_type": ["Task"],
            "doc_name": ["TASK-NOISE"],
            "title": ["Contrato"],
            "content": ["sin relacion"],
        },
        "useful-partial": {
            "doc_type": ["Task"],
            "doc_name": ["TASK-PARTIAL"],
            "title": ["Contrato cliente"],
            "content": ["seguimiento"],
        },
    }

    class FakeSearcher:
        def search(self, query, limit, count=True, offset=0):
            hits = {
                "strict": [(2.0, "exact"), (20.0, "generic")],
                "relaxed": [
                    (2.0, "exact"),
                    (8.0, "too-partial"),
                    (5.0, "useful-partial"),
                ],
            }[query]
            return SearchResult(hits[offset:offset + limit], len(hits))

        def doc(self, address):
            return documents[address]

    class FakeIndex:
        def reload(self):
            return None

        def searcher(self):
            return FakeSearcher()

    monkeypatch.setattr(tantivy_backend, "get_index", lambda: FakeIndex())
    monkeypatch.setattr(
        tantivy_backend,
        "build_natural_query",
        lambda index, query, fields, require_all: (
            "strict" if require_all else "relaxed"
        ),
    )

    results = tantivy_backend.search(
        "contrato cliente vencido urgente",
        limit=3,
    )

    assert [row["doc_name"] for row in results] == [
        "TASK-EXACT",
        "TASK-GENERIC",
        "TASK-PARTIAL",
    ]
    assert results[0]["match_type"] == "exact"

    second_page = tantivy_backend.search(
        "contrato cliente vencido urgente",
        limit=2,
        offset=1,
    )
    assert [row["doc_name"] for row in second_page] == [
        "TASK-GENERIC",
        "TASK-PARTIAL",
    ]


def test_tantivy_rebuild_publishes_generation_atomically(monkeypatch, tmp_path):
    class FakeSchemaBuilder:
        def add_text_field(self, *args, **kwargs):
            return None

        def build(self):
            return "schema"

    class FakeDocument:
        def __init__(self):
            self.values = {}

        def add_text(self, field, value):
            self.values[field] = value

    class FakeWriter:
        def __init__(self, path):
            self.path = path
            self.documents = []

        def add_document(self, document):
            self.documents.append(document)

        def commit(self):
            (self.path / "documents.json").write_text(
                json.dumps([doc.values for doc in self.documents]),
                encoding="utf-8",
            )

    class FakeIndex:
        def __init__(self, schema, path):
            self.path = Path(path)

        def writer(self):
            return FakeWriter(self.path)

    fake_tantivy = SimpleNamespace(
        SchemaBuilder=FakeSchemaBuilder,
        Document=FakeDocument,
        Index=FakeIndex,
    )
    hashes = iter(["temp123", "generation123", "pointer1"])
    monkeypatch.setitem(sys.modules, "tantivy", fake_tantivy)
    monkeypatch.setattr(
        tantivy_backend,
        "_root_path",
        lambda: tmp_path / "tantivy_v3",
    )
    monkeypatch.setattr(
        tantivy_backend.frappe,
        "generate_hash",
        lambda length: next(hashes),
    )

    result = tantivy_backend.rebuild(
        [
            {
                "doc_type": "Customer",
                "doc_name": "CUS-001",
                "title": "Cliente Nómina",
                "content": "Contrato activo",
            }
        ]
    )

    pointer = json.loads(
        (tmp_path / "tantivy_v3" / "current.json").read_text(
            encoding="utf-8"
        )
    )
    index_file = (
        tmp_path
        / "tantivy_v3"
        / "generations"
        / pointer["generation"]
        / "tantivy"
        / "documents.json"
    )
    indexed = json.loads(index_file.read_text(encoding="utf-8"))
    assert result["count"] == 1
    assert pointer["generation"] == "gen-generation123"
    assert indexed[0]["doc_name_normalized"] == "cus-001"
    assert indexed[0]["title_normalized"] == "cliente nomina"

def test_live_field_comment_attachment_and_child_permissions(monkeypatch, tmp_path):
    import pytest
    from document_management.search import indexer

    def throw(message, exc=None):
        raise (exc or AssertionError)(message)

    monkeypatch.setattr(indexer.frappe.local, "flags", SimpleNamespace(in_test=False), raising=False)
    monkeypatch.setattr(indexer.frappe, "_", lambda text: text)
    monkeypatch.setattr(indexer.frappe, "throw", throw)

    class Source(Row):
        def __init__(self, doctype, name, fields=(), *, allowed=True, **values):
            super().__init__(fields, **values)
            self.doctype, self.name, self.allowed = doctype, name, allowed
            self.modified, self.owner = "2026-11-27", "search-test"
            self.grants = {f.fieldname for f in fields}
            self.meta = SimpleNamespace(
                fields=fields, title_field=None, istable=False,
                get_field=lambda name: next((f for f in fields if f.fieldname == name), None),
                get_permitted_fieldnames=lambda **kwargs: list(self.grants),
            )

        def has_permission(self, *args, **kwargs):
            return self.allowed

    parent = Source("Task", "TASK-1", [
        _field("title", "Data"), _field("private_note", "Data", permlevel=1), _field("omitted", "Data"),
        _field("items", "Table", options="Task Item", hidden=0), _field("attachment", "Attach"),
    ], title="Visible title", private_note="RestrictedField", omitted="OmittedSetting")
    parent.meta.title_field = "title"
    parent.grants.remove("private_note")
    children = [
        Source("Task Item", "ROW-1", [_field("entry", "Data")], entry="VisibleChild"),
        Source("Task Item", "ROW-2", [_field("entry", "Data")], entry="DeniedChild", allowed=False),
    ]
    for n, child in enumerate(children, 1):
        child.meta.istable = True
        child.parenttype, child.parent, child.parentfield, child.idx = "Task", parent.name, "items", n
    parent["items"] = children
    comments = [
        Source("Comment", "C1", [_field("content", "Text")], content="VisibleComment"),
        Source("Comment", "C2", [_field("content", "Text")], content="DeniedComment", allowed=False),
    ]
    for comment in comments:
        comment.update(name=comment.name, reference_name=parent.name)
    root = tmp_path / "private" / "files"
    root.mkdir(parents=True)
    files = []
    for name, text, allowed in (("F1", "VisibleAttachment", True), ("F2", "DeniedAttachment", False)):
        path = root / (name + ".txt")
        path.write_text(text)
        file = Source("File", name, [_field("file_name", "Data"), _field("file_url", "Data")], allowed=allowed)
        file.file_name = path.name
        file.is_private = True
        file.get_full_path = lambda path=path: str(path)
        file.update(name=name, file_url="/private/files/" + path.name, attached_to_field="attachment", is_private=1)
        files.append(file)
    docs = {(d.doctype, d.name): d for d in [parent, *children, *comments, *files]}
    rules = {"Task": {"excluded_fields": ["omitted"], "index_comments": True, "index_attachments": True}}
    monkeypatch.setattr(indexer, "_get_indexing_config", lambda **kw: (True, False, set(rules), rules))
    monkeypatch.setattr(indexer, "_can_offer_doctype", lambda dt: True)
    monkeypatch.setattr(indexer, "get_url_to_form", lambda dt, name: f"/app/{dt.lower()}/{name}")
    monkeypatch.setattr(indexer.frappe, "get_site_path", lambda *p: str(tmp_path.joinpath(*p)))
    monkeypatch.setattr(indexer.frappe, "get_meta", lambda dt: next(d.meta for d in docs.values() if d.doctype == dt), raising=False)
    monkeypatch.setattr(indexer.frappe, "get_doc", lambda dt, name: docs[(dt, name)])
    monkeypatch.setattr(indexer.frappe, "get_list", lambda dt, **kw: [name for kind, name in docs if kind == dt], raising=False)
    monkeypatch.setattr(indexer.frappe, "get_all", lambda dt, **kw: {"File": files, "Comment": comments}[dt])
    monkeypatch.setattr(indexer.frappe, "session", SimpleNamespace(user="reader@example.test"))
    monkeypatch.setattr(indexer.tantivy_backend, "iter_search", lambda *a, **kw: iter([
        [{"doc_type": "Task", "doc_name": parent.name, "_raw_content": "OldPrivateSnapshot"}]
    ]))

    def rows(query):
        result = indexer.search(query)
        assert "exact_error" not in result
        return result["exact"]

    for token in ("OldPrivateSnapshot", "RestrictedField", "DeniedChild", "DeniedComment", "DeniedAttachment", "OmittedSetting"):
        assert rows(token) == []
    assert rows('"visible title" -restrictedfield')
    for token in ("VisibleChild", "VisibleComment", "VisibleAttachment"):
        assert token in rows(token)[0]["excerpt"]
    assert "OldPrivateSnapshot" not in str(rows("Visible"))
    parent.grants.add("private_note")
    assert rows('"visible title" -restrictedfield') == []
    assert "RestrictedField" in rows("RestrictedField")[0]["excerpt"]
    preview = indexer.get_search_result_preview("Task", parent.name, "VisibleAttachment")
    assert [file["title"] for file in preview["matched_attachments"]] == ["F1.txt"]
    rules["Task"].update(index_comments=False, index_attachments=False)
    assert not rows("VisibleComment") and not rows("VisibleAttachment")
    child_preview = indexer.get_search_result_preview("Task Item", "ROW-1", "VisibleChild")
    assert child_preview["route"] == "/app/task/TASK-1"
    assert child_preview["matched_child_rows"][0]["row_name"] == "ROW-1"
    with pytest.raises(indexer.frappe.PermissionError):
        indexer.get_search_result_preview("Task Item", "ROW-2", "DeniedChild")
    parent.grants.remove("items")
    with pytest.raises(indexer.frappe.PermissionError):
        indexer.get_search_result_preview("Task Item", "ROW-1", "VisibleChild")
    assert not rows("VisibleChild")


def test_sorted_pages_cover_all_readable_hits_and_break_ties(monkeypatch):
    from document_management.search import indexer

    rules = {"Task": {}, "Project": {}}
    monkeypatch.setattr(indexer.frappe.local, "flags", SimpleNamespace(in_test=False), raising=False)
    monkeypatch.setattr(indexer.frappe, "session", SimpleNamespace(user="reader@example.test"))
    monkeypatch.setattr(indexer, "_get_indexing_config", lambda **kw: (True, False, set(rules), rules))
    monkeypatch.setattr(indexer, "_can_offer_doctype", lambda dt: True)
    # The last candidates in relevance order sort onto the first pages by date and title.
    hits = [{"doc_type": ["Task", "Project"][n % 2], "doc_name": f"ROW-{(n // 2):04d}",
             "title": "A" if n >= 500 else "B", "modified": f"2026-11-{1 + (n % 26):02d}",
             "allowed": n % 73 != 0} for n in range(522)]
    hits[-1]["modified"] = "2001-01-01"
    hits[-2]["modified"] = "2030-01-01"
    monkeypatch.setattr(indexer.tantivy_backend, "iter_search", lambda *a, **kw: (hits[i:i + 41] for i in range(0, len(hits), 41)))
    allowed = [row for row in hits if row["allowed"]]
    assert len(allowed) > 500
    for match_type in ("all_terms", "fuzzy"):
        monkeypatch.setattr(indexer, "_visible_result", lambda hit, *a: dict(hit, match_type=match_type) if hit["allowed"] else None)
        for order in ("title_asc", "modified_asc", "modified_desc"):
            field = "title" if order == "title_asc" else "modified"
            expected = sorted(allowed, key=lambda row: (row[field], row["doc_type"], row["doc_name"]), reverse=order == "modified_desc")
            for page in (1, 11):
                result = indexer.search("record", page=page, page_length=50, sort_by=order)
                rows = result["exact"]
                offset = (page - 1) * 50
                assert [(r["doc_type"], r["doc_name"]) for r in rows] == [
                    (r["doc_type"], r["doc_name"]) for r in expected[offset:offset + 50]
                ]
                assert sum(result["facets"].values()) == len(expected)
                assert not result["facets_truncated"]
                assert result["pagination"]["has_more"] == (offset + 50 < len(expected))
