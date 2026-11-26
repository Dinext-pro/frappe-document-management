import os
import tempfile

import frappe

from document_management.frappe_document_management.rag.config import standalone_ai_disabled
from document_management.frappe_document_management.utils.document_provider import is_standalone, standalone_filters

from document_management.search.query import (
    ENGLISH_STOPWORDS,
    FRENCH_STOPWORDS,
    SPANISH_STOPWORDS,
)


MULTILINGUAL_STOPWORDS = sorted(
    ENGLISH_STOPWORDS | SPANISH_STOPWORDS | FRENCH_STOPWORDS
)


def _model_paths():
    model_dir = frappe.utils.get_site_path("private", "files", "models")
    return (
        model_dir,
        os.path.join(model_dir, "tagger_model.joblib"),
        os.path.join(model_dir, "tagger_vectorizer.joblib"),
        os.path.join(model_dir, "tagger_mlb.joblib"),
    )


def _atomic_joblib_dump(joblib_module, obj, target_path):
    directory = os.path.dirname(target_path)
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".tagger-",
        suffix=".joblib",
        dir=directory,
    )
    os.close(fd)
    try:
        joblib_module.dump(obj, temp_path)
        os.replace(temp_path, target_path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def train_tagger_model():
    if standalone_ai_disabled():
        return False
    try:
        import joblib
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.multiclass import OneVsRestClassifier
        from sklearn.preprocessing import MultiLabelBinarizer
        from sklearn.svm import LinearSVC
    except ImportError:
        frappe.logger("document_ml_tagger").warning(
            "ML Tagger skipped: scikit-learn or joblib is not installed."
        )
        return False

    try:
        ml_tags = frappe.get_all(
            "Document Tag",
            filters={
                "matching_algorithm": "Auto (Machine Learning)",
                "is_active": 1,
            },
            pluck="name",
            limit_page_length=0,
        )
        if not ml_tags:
            return False

        docs = frappe.get_all(
            "Document",
            filters=standalone_filters({"ocr_status": "Completed", "is_deleted": 0}),
            fields=["name", "ocr_content"],
            limit_page_length=0,
        )
        if not docs:
            return False

        doc_names = [d.name for d in docs if d.ocr_content]
        if not doc_names:
            return False

        tag_links = frappe.get_all(
            "Document Tag Link",
            filters={"parenttype": "Document", "parent": ["in", doc_names]},
            fields=["parent", "tag"],
            limit_page_length=0,
        )
        tags_by_doc = {}
        for link in tag_links:
            if link.tag:
                tags_by_doc.setdefault(link.parent, []).append(link.tag)

        X_texts = []
        Y_labels = []
        for d in docs:
            if not d.ocr_content:
                continue
            doc_tags = tags_by_doc.get(d.name)
            if not doc_tags:
                continue
            X_texts.append(d.ocr_content)
            Y_labels.append(doc_tags)

        if len(X_texts) < 2:
            return False

        vectorizer = TfidfVectorizer(
            max_features=5000,
            stop_words=MULTILINGUAL_STOPWORDS,
        )
        X = vectorizer.fit_transform(X_texts)

        mlb = MultiLabelBinarizer()
        Y = mlb.fit_transform(Y_labels)

        # Filter out degenerate classes that have only one value across all samples
        valid_cols = [
            idx
            for idx in range(Y.shape[1])
            if 0 < int(Y[:, idx].sum()) < Y.shape[0]
        ]
        if not valid_cols:
            return False

        if len(valid_cols) != Y.shape[1]:
            keep_classes = set(mlb.classes_[valid_cols])
            filtered_labels = [
                [tag for tag in tags if tag in keep_classes]
                for tags in Y_labels
            ]
            mlb = MultiLabelBinarizer()
            Y = mlb.fit_transform(filtered_labels)

        classifier = OneVsRestClassifier(LinearSVC(class_weight="balanced"))
        classifier.fit(X, Y)

        model_dir, model_path, vectorizer_path, mlb_path = _model_paths()
        os.makedirs(model_dir, exist_ok=True)
        _atomic_joblib_dump(joblib, classifier, model_path)
        _atomic_joblib_dump(joblib, vectorizer, vectorizer_path)
        _atomic_joblib_dump(joblib, mlb, mlb_path)

        frappe.logger("document_ml_tagger").info(
            "ML Tagger trained model with %d documents and %d tags.",
            len(X_texts),
            len(mlb.classes_),
        )
        return True

    except Exception:
        frappe.log_error(
            title="ML Tagger Training Failed",
            message=frappe.get_traceback(),
        )
        return False


def predict_tags(doc, extracted_text):
    if not extracted_text or standalone_ai_disabled() or not is_standalone(doc):
        return False

    try:
        import joblib
    except ImportError:
        return False

    _, model_path, vectorizer_path, mlb_path = _model_paths()
    if (
        not os.path.exists(model_path)
        or not os.path.exists(vectorizer_path)
        or not os.path.exists(mlb_path)
    ):
        return False

    try:
        ml_tags = set(
            frappe.get_all(
                "Document Tag",
                filters={
                    "matching_algorithm": "Auto (Machine Learning)",
                    "is_active": 1,
                },
                pluck="name",
                limit_page_length=0,
            )
        )
        if not ml_tags:
            return False

        classifier = joblib.load(model_path)
        vectorizer = joblib.load(vectorizer_path)
        mlb = joblib.load(mlb_path)

        X = vectorizer.transform([extracted_text])
        predicted_indices = classifier.predict(X)
        predicted_tags = mlb.inverse_transform(predicted_indices)[0]

        existing_tags = {t.tag for t in doc.get("tags", [])}
        tags_added = False

        for tag in predicted_tags:
            if tag in ml_tags and tag not in existing_tags:
                doc.append("tags", {"tag": tag})
                existing_tags.add(tag)
                tags_added = True

        return tags_added

    except Exception:
        frappe.log_error(
            title="ML Tagger Prediction Failed",
            message=f"Doc: {doc.name}\nError: {frappe.get_traceback()}",
        )
        return False
