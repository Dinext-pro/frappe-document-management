import json
import re
import unicodedata


SPANISH_STOPWORDS = frozenset(
    {
        "a",
        "al",
        "ante",
        "bajo",
        "con",
        "contra",
        "como",
        "cómo",
        "cual",
        "cuál",
        "cuales",
        "cuáles",
        "cuando",
        "cuándo",
        "cuanto",
        "cuánto",
        "de",
        "del",
        "desde",
        "donde",
        "dónde",
        "durante",
        "e",
        "el",
        "ella",
        "ellas",
        "ellos",
        "en",
        "entre",
        "era",
        "es",
        "esa",
        "ese",
        "eso",
        "esta",
        "este",
        "esto",
        "fue",
        "ha",
        "hay",
        "hacia",
        "hasta",
        "la",
        "las",
        "lo",
        "los",
        "más",
        "mi",
        "ni",
        "no",
        "o",
        "para",
        "pero",
        "por",
        "que",
        "qué",
        "quien",
        "quién",
        "quienes",
        "quiénes",
        "se",
        "según",
        "ser",
        "sin",
        "son",
        "sobre",
        "su",
        "sus",
        "tras",
        "tu",
        "tiene",
        "tienen",
        "un",
        "una",
        "uno",
        "unos",
        "unas",
        "y",
        "ya",
    }
)

ENGLISH_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "me",
        "more",
        "most",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
    }
)

FRENCH_STOPWORDS = frozenset(
    {
        "a",
        "ai",
        "aie",
        "aient",
        "ait",
        "as",
        "au",
        "aucun",
        "aucune",
        "aux",
        "avec",
        "avez",
        "avons",
        "ayant",
        "ayez",
        "c",
        "car",
        "ce",
        "ces",
        "cet",
        "cette",
        "d",
        "dans",
        "de",
        "des",
        "du",
        "elle",
        "elles",
        "en",
        "entre",
        "es",
        "est",
        "et",
        "eu",
        "eue",
        "eues",
        "eurent",
        "eus",
        "eusse",
        "eussent",
        "eusses",
        "eussiez",
        "eussions",
        "eut",
        "eût",
        "eûmes",
        "eûtes",
        "faire",
        "fait",
        "faites",
        "fois",
        "font",
        "furent",
        "fus",
        "fusse",
        "fussent",
        "fusses",
        "fussiez",
        "fussions",
        "fut",
        "fûmes",
        "fût",
        "fûtes",
        "ici",
        "il",
        "ils",
        "j",
        "je",
        "l",
        "la",
        "le",
        "les",
        "leur",
        "leurs",
        "lui",
        "m",
        "ma",
        "mais",
        "me",
        "mes",
        "moi",
        "mon",
        "n",
        "ne",
        "ni",
        "nos",
        "notre",
        "nous",
        "on",
        "ont",
        "ou",
        "où",
        "par",
        "pas",
        "pour",
        "qu",
        "que",
        "quel",
        "quelle",
        "quelles",
        "quels",
        "qui",
        "s",
        "sa",
        "sans",
        "se",
        "sera",
        "serai",
        "seraient",
        "serais",
        "serait",
        "seras",
        "serez",
        "seriez",
        "serions",
        "serons",
        "seront",
        "ses",
        "soi",
        "soient",
        "sois",
        "soit",
        "sommes",
        "son",
        "sont",
        "soyez",
        "soyons",
        "suis",
        "sur",
        "t",
        "ta",
        "te",
        "tes",
        "toi",
        "ton",
        "tu",
        "un",
        "une",
        "vos",
        "votre",
        "vous",
        "y",
        "à",
        "ça",
        "étaient",
        "étais",
        "était",
        "étant",
        "étiez",
        "étions",
        "été",
        "étée",
        "étées",
        "étés",
        "êtes",
    }
)

STOPWORDS_BY_LANGUAGE = {
    "en": ENGLISH_STOPWORDS,
    "es": SPANISH_STOPWORDS,
    "fr": FRENCH_STOPWORDS,
}

LANGUAGE_ALIASES = {
    "english": "en",
    "espanol": "es",
    "french": "fr",
    "francais": "fr",
    "spanish": "es",
}

TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
QUOTED_PHRASE_RE = re.compile(r'"([^"]+)"')
EXCLUDED_TERM_RE = re.compile(r'(?:^|\s)-([^\W_]{2,})', re.UNICODE)


def parse_search_syntax(query):
    """Extract quoted phrases ("...") and negative terms (-word) from a query."""
    raw = str(query or "")
    phrases = [
        " ".join(query_tokens(match.group(1)))
        for match in QUOTED_PHRASE_RE.finditer(raw)
        if query_tokens(match.group(1))
    ]
    without_phrases = QUOTED_PHRASE_RE.sub(" ", raw)
    excluded = list(
        dict.fromkeys(
            match.group(1).lower()
            for match in EXCLUDED_TERM_RE.finditer(without_phrases)
        )
    )
    positive_text = EXCLUDED_TERM_RE.sub(" ", without_phrases)
    return {
        "phrases": list(dict.fromkeys(phrases)),
        "excluded": excluded,
        "positive_text": " ".join(positive_text.split()),
    }


def positive_query_text(query):
    """Return query text stripped of -excluded terms for scoring, excerpts, and highlights."""
    parsed = parse_search_syntax(query)
    if not parsed["phrases"] and not parsed["excluded"]:
        return str(query or "")
    return f'{parsed["positive_text"]} {" ".join(parsed["phrases"])}'.strip()


def normalize_language(language):
    normalized = str(language or "").strip().lower().replace("_", "-")
    code = normalized.split("-", 1)[0]
    if code in STOPWORDS_BY_LANGUAGE:
        return code

    name = re.split(r"[\s(]", normalized, maxsplit=1)[0]
    folded_name = "".join(
        character
        for character in unicodedata.normalize("NFKD", name)
        if not unicodedata.combining(character)
    )
    return LANGUAGE_ALIASES.get(folded_name, code)


def current_search_language():
    try:
        import frappe
    except Exception:
        return ""

    language = None
    try:
        language = getattr(frappe.local, "lang", None)
    except Exception:
        pass
    if not language:
        try:
            user = getattr(getattr(frappe, "session", None), "user", None)
            if user and user != "Guest":
                language = frappe.db.get_value("User", user, "language")
        except Exception:
            pass
    if not language:
        try:
            language = frappe.db.get_single_value("System Settings", "language")
        except Exception:
            pass
    return normalize_language(language)


def stopwords_for_language(language=None):
    code = normalize_language(language) if language else current_search_language()
    return STOPWORDS_BY_LANGUAGE.get(code, frozenset())


def query_tokens(query):
    return TOKEN_RE.findall((query or "").lower())


def fold_text(value):
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(character)
    ).lower()


def _fold_with_offsets(value):
    folded = []
    offsets = []
    for index, character in enumerate(str(value or "")):
        normalized = unicodedata.normalize("NFKD", character)
        for normalized_character in normalized:
            if unicodedata.combining(normalized_character):
                continue
            folded.append(normalized_character.lower())
            offsets.append(index)
    return "".join(folded), offsets


def significant_terms(query, language=None):
    stopwords = stopwords_for_language(language)
    return list(
        dict.fromkeys(
            token
            for token in query_tokens(query)
            if len(token) > 1 and token not in stopwords
        )
    )


def make_excerpt(content, query, maximum=360, language=None):
    normalized = " ".join((content or "").split())
    if len(normalized) <= maximum:
        return normalized

    positive = positive_query_text(query)
    folded_content, offsets = _fold_with_offsets(normalized)
    positions = [
        folded_content.find(fold_text(term))
        for term in significant_terms(positive, language=language)
    ]
    positions = [position for position in positions if position >= 0]
    center = offsets[min(positions)] if positions and offsets else 0
    start = max(center - maximum // 3, 0)
    end = min(start + maximum, len(normalized))
    if end - start < maximum:
        start = max(end - maximum, 0)
    prefix = "..." if start else ""
    suffix = "..." if end < len(normalized) else ""
    return prefix + normalized[start:end].strip() + suffix


def search_match_details(query, doc_name="", title="", content="", language=None):
    positive = positive_query_text(query)
    terms = significant_terms(positive, language=language)
    folded_terms = list(dict.fromkeys(fold_text(term) for term in terms))
    folded_query = " ".join(fold_text(positive).split())
    folded_name = " ".join(fold_text(doc_name).split())
    folded_title = " ".join(fold_text(title).split())
    folded_content = fold_text(content)
    searchable = " ".join(
        part for part in (folded_name, folded_title, folded_content) if part
    )
    searchable_tokens = set(query_tokens(searchable))
    matched_terms = sum(term in searchable_tokens for term in folded_terms)
    total_terms = len(folded_terms)
    coverage = matched_terms / total_terms if total_terms else 0.0

    exact_name = bool(folded_query and folded_name == folded_query)
    exact_title = bool(folded_query and folded_title == folded_query)
    quality = 0
    if exact_name:
        quality = 100
    elif exact_title:
        quality = 90
    elif folded_query and folded_name.startswith(folded_query):
        quality = 75
    elif folded_query and folded_title.startswith(folded_query):
        quality = 65
    elif folded_query and folded_query in folded_title:
        quality = 50
    elif folded_query and folded_query in folded_content:
        quality = 30

    if exact_name or exact_title:
        match_type = "exact"
    elif total_terms and matched_terms == total_terms:
        match_type = "all_terms"
    else:
        match_type = "partial"

    return {
        "quality": quality,
        "match_type": match_type,
        "matched_terms": matched_terms,
        "total_terms": total_terms,
        "coverage": coverage,
    }


def build_natural_query(
    index,
    query,
    fields,
    language=None,
    require_all=True,
    allow_prefix=True,
    fuzzy=False,
):
    import tantivy

    parsed_syntax = parse_search_syntax(query)
    positive_source = (
        f'{parsed_syntax["positive_text"]} {" ".join(parsed_syntax["phrases"])}'.strip()
        if (parsed_syntax["phrases"] or parsed_syntax["excluded"])
        else str(query or "")
    )
    tokens = query_tokens(positive_source)
    terms = significant_terms(positive_source, language=language)
    if not terms:
        terms = list(dict.fromkeys(tokens))
    if not terms and not parsed_syntax["phrases"]:
        return tantivy.Query.empty_query()

    field_weights = (
        fields
        if isinstance(fields, dict)
        else {field: 1.0 for field in fields}
    )
    schema = getattr(index, "schema", None)
    has_regex = allow_prefix and hasattr(tantivy.Query, "regex_query") and schema is not None
    has_fuzzy = fuzzy and hasattr(tantivy.Query, "fuzzy_term_query") and schema is not None
    must_not_occur = getattr(tantivy.Occur, "MustNot", None)

    clauses = []
    for term in terms:
        variants = list(dict.fromkeys([term, fold_text(term)]))
        alternatives = []
        for field, weight in field_weights.items():
            for variant in variants:
                if has_fuzzy and len(variant) >= 4:
                    try:
                        fq = tantivy.Query.fuzzy_term_query(
                            schema,
                            field,
                            variant,
                            distance=1,
                            transposition_cost_one=True,
                            prefix=False,
                        )
                        if weight != 1:
                            fq = tantivy.Query.boost_query(fq, float(weight) * 0.75)
                        alternatives.append((tantivy.Occur.Should, fq))
                    except Exception:
                        pass
                else:
                    parsed = index.parse_query(json.dumps(variant), [field])
                    if weight != 1:
                        parsed = tantivy.Query.boost_query(parsed, float(weight))
                    alternatives.append((tantivy.Occur.Should, parsed))
                    if (
                        has_regex
                        and (
                            (
                                len(variant) >= 3
                                and field in {"doc_name", "doc_name_normalized"}
                            )
                            or (
                                len(variant) >= 4
                                and field in {"title", "title_normalized"}
                            )
                        )
                    ):
                        try:
                            pq = tantivy.Query.regex_query(
                                schema,
                                field,
                                f"{re.escape(variant)}.*",
                            )
                            pq = tantivy.Query.boost_query(pq, max(float(weight) * 0.6, 0.5))
                            alternatives.append((tantivy.Occur.Should, pq))
                        except Exception:
                            pass
        if not alternatives:
            continue
        term_query = (
            alternatives[0][1]
            if len(alternatives) == 1
            else tantivy.Query.boolean_query(alternatives)
        )
        clauses.append(
            (
                tantivy.Occur.Must if require_all else tantivy.Occur.Should,
                term_query,
            )
        )

    # Explicit user-quoted phrases must match in at least one field
    for user_phrase in parsed_syntax["phrases"]:
        phrase_variants = list(dict.fromkeys([user_phrase, fold_text(user_phrase)]))
        phrase_alts = []
        for field, weight in field_weights.items():
            for phrase_text in phrase_variants:
                phrase_q = index.parse_query(f'"{phrase_text}"', [field])
                phrase_q = tantivy.Query.boost_query(
                    phrase_q,
                    max(float(weight) * 2.5, 2.5),
                )
                phrase_alts.append((tantivy.Occur.Should, phrase_q))
        if phrase_alts:
            clauses.append(
                (
                    tantivy.Occur.Must,
                    phrase_alts[0][1]
                    if len(phrase_alts) == 1
                    else tantivy.Query.boolean_query(phrase_alts),
                )
            )

    if len(tokens) > 1 and not fuzzy:
        phrases = list(
            dict.fromkeys(
                [
                    " ".join(tokens),
                    fold_text(" ".join(tokens)),
                ]
            )
        )
        for field, weight in field_weights.items():
            for phrase_text in phrases:
                phrase = index.parse_query(f'"{phrase_text}"', [field])
                clauses.append(
                    (
                        tantivy.Occur.Should,
                        tantivy.Query.boost_query(
                            phrase,
                            max(float(weight) * 2.0, 2.0),
                        ),
                    )
                )

    if must_not_occur is not None:
        for excluded_term in parsed_syntax["excluded"]:
            variants = list(dict.fromkeys([excluded_term, fold_text(excluded_term)]))
            for field in field_weights:
                for variant in variants:
                    clauses.append(
                        (
                            must_not_occur,
                            index.parse_query(json.dumps(variant), [field]),
                        )
                    )

    if not clauses:
        return tantivy.Query.empty_query()
    if len(clauses) == 1:
        return clauses[0][1]
    return tantivy.Query.boolean_query(clauses)
