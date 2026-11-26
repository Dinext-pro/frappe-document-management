frappe.ui.form.on('Document Management Settings', {
    setup(frm) {
        frm.set_query('document_type', 'indexed_doctypes', () => {
            return {
                filters: {
                    istable: 0,
                    issingle: 0
                }
            };
        });
    },

    refresh(frm) {
        render_provider_summary(frm);
        frm.add_custom_button(__('Check System Health'), () => {
            frappe.call({
                method: 'document_management.frappe_document_management.doctype.document_management_settings.document_management_settings.check_system_health',
                freeze: true,
                callback: (r) => {
                    const data = r.message || {};
                    const escape = (val) => $('<div>').text(val == null ? '' : String(val)).html();
                    const badge = (ok) => ok
                        ? '<span class="indicator-pill green">' + __('OK') + '</span>'
                        : '<span class="indicator-pill red">' + __('Missing / Error') + '</span>';
                    const binaries = Object.entries(data.binaries || {})
                        .map(([name, ok]) => `<tr><td><code>${escape(name)}</code></td><td>${badge(ok)}</td></tr>`)
                        .join('');
                    const endpoints = Object.entries(data.endpoints || {})
                        .filter(([, info]) => info && typeof info === 'object')
                        .map(([label, info]) => `<tr><td>${escape(label)} (${escape(info.provider)})</td><td>${badge(info.ok)} ${escape(info.error || info.detail || info.endpoint || '')}</td></tr>`)
                        .join('');
                    const rag = data.rag_index || {};
                    const ingestion = data.folder_ingestion || {};
                    const ingestionHtml = ingestion.enabled
                        ? `<p>${__('Folder Ingestion')}: ${badge(Boolean(ingestion.ok))} ${escape(ingestion.error || ingestion.path || '')}</p>`
                        : `<p>${__('Folder Ingestion')}: <span class="text-muted">${__('Disabled')}</span></p>`;
                    frappe.msgprint({
                        title: __('Document Management System Health'),
                        wide: true,
                        message: `
                            <h5>${__('System Binaries')}</h5>
                            <table class="table table-bordered table-condensed"><tbody>${binaries}</tbody></table>
                            <h5>${__('Search & RAG Indexes')}</h5>
                            <p>${__('Full-text (Tantivy)')}: ${badge(Boolean(data.tantivy_ready))}</p>
                            <p>${__('RAG Index')}: ${badge(Boolean(rag.valid))} ${escape(rag.error || (rag.chunks != null ? `${rag.chunks} chunks` : ''))}</p>
                            <h5>${__('Storage & Ingestion')}</h5>
                            <p>${__('File Encryption at Rest')}: ${data.encryption_enabled ? '<span class="indicator-pill green">' + __('Enabled') + '</span>' : '<span class="text-muted">' + __('Disabled') + '</span>'}</p>
                            ${ingestionHtml}
                            <h5>${__('AI Endpoints')}</h5>
                            <table class="table table-bordered table-condensed"><tbody>${endpoints}</tbody></table>
                        `
                    });
                }
            });
        }, __('Maintenance'));

        frm.add_custom_button(__('Run Archive Sanity Check'), () => {
            frappe.call({
                method: 'document_management.frappe_document_management.utils.archive_sanity.run_document_archive_sanity_check',
                freeze: true,
                freeze_message: __('Checking document archive integrity...'),
                callback: (r) => {
                    const data = r.message || {};
                    const escape = (val) => $('<div>').text(val == null ? '' : String(val)).html();
                    const counts = data.counts || {error: 0, warning: 0};
                    const issues = (data.issues || []).slice(0, 100);
                    const rows = issues.map((item) => `
                        <tr>
                            <td><span class="indicator-pill ${item.severity === 'error' ? 'red' : 'orange'}">${escape(item.severity)}</span></td>
                            <td><code>${escape(item.code)}</code></td>
                            <td>${escape(item.document || item.file || '')}</td>
                            <td>${escape(item.message)}</td>
                        </tr>
                    `).join('');
                    const truncated = (data.issues || []).length > issues.length
                        ? `<p class="text-muted">${__('Showing first 100 issues.')}</p>`
                        : '';
                    frappe.msgprint({
                        title: __('Archive Sanity Check'),
                        indicator: counts.error ? 'red' : (counts.warning ? 'orange' : 'green'),
                        wide: true,
                        message: `
                            <p>
                                <strong>${__('Documents checked')}:</strong> ${escape(data.documents_checked || 0)} &middot;
                                <strong>${__('Versions checked')}:</strong> ${escape(data.versions_checked || 0)} &middot;
                                <strong>${__('Errors')}:</strong> ${escape(counts.error || 0)} &middot;
                                <strong>${__('Warnings')}:</strong> ${escape(counts.warning || 0)}
                            </p>
                            ${issues.length
                                ? `<table class="table table-bordered table-condensed"><thead><tr><th>${__('Severity')}</th><th>${__('Code')}</th><th>${__('Target')}</th><th>${__('Message')}</th></tr></thead><tbody>${rows}</tbody></table>${truncated}`
                                : `<div class="alert alert-success">${__('No archive integrity issues found.')}</div>`}
                        `
                    });
                }
            });
        }, __('Maintenance'));

        if (frm.doc.enable_file_encryption) {
            frm.add_custom_button(__('Encrypt Existing Files'), () => {
                frappe.confirm(
                    __('Encrypt all unencrypted private Document files in the background using the site encryption key?'),
                    () => {
                        frappe.call({
                            method: 'document_management.frappe_document_management.utils.file_crypto.enqueue_encrypt_existing_documents',
                            freeze: true,
                            callback: () => {
                                frappe.show_alert({
                                    message: __('Background encryption job queued'),
                                    indicator: 'blue'
                                });
                            }
                        });
                    }
                );
            }, __('Maintenance'));
        }
    },

    chat_provider(frm) {
        const model = frm.doc.rag_model || '';
        if (
            frm.doc.chat_provider === 'Ollama'
            && (!model || ['gpt-5-mini', 'local-model'].includes(model))
        ) {
            frm.set_value('rag_model', 'llama3.1');
        } else if (
            frm.doc.chat_provider === 'LM Studio'
            && (!model || ['gpt-5-mini', 'llama3.1'].includes(model))
        ) {
            frm.set_value('rag_model', 'local-model');
            if (!frm.doc.chat_endpoint) {
                frm.set_value('chat_endpoint', 'http://localhost:1234/v1');
            }
        } else if (
            ['OpenAI', 'OpenAI Compatible'].includes(frm.doc.chat_provider)
            && (!model || ['llama3.1', 'local-model'].includes(model))
        ) {
            frm.set_value('rag_model', 'gpt-5-mini');
        }
        render_provider_summary(frm);
    },

    chat_endpoint: render_provider_summary,
    rag_model: render_provider_summary,

    embedding_provider(frm) {
        const model = frm.doc.embedding_model || '';
        if (
            ['OpenAI', 'OpenAI Compatible'].includes(frm.doc.embedding_provider)
            && (
                !model
                || [
                    'sentence-transformers/all-MiniLM-L6-v2',
                    'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
                    'local-model'
                ].includes(model)
            )
        ) {
            frm.set_value('embedding_model', 'text-embedding-3-small');
        } else if (
            frm.doc.embedding_provider === 'LM Studio'
            && (
                !model
                || [
                    'sentence-transformers/all-MiniLM-L6-v2',
                    'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
                    'text-embedding-3-small'
                ].includes(model)
            )
        ) {
            frm.set_value('embedding_model', 'local-model');
            if (!frm.doc.embedding_endpoint) {
                frm.set_value('embedding_endpoint', 'http://localhost:1234/v1');
            }
        } else if (
            frm.doc.embedding_provider === 'Local (sentence-transformers)'
            && (!model || ['text-embedding-3-small', 'local-model'].includes(model))
        ) {
            frm.set_value(
                'embedding_model',
                'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
            );
        }
        render_provider_summary(frm);
    },

    embedding_endpoint: render_provider_summary,
    embedding_model: render_provider_summary,
    enable_openai_for_ocr: render_provider_summary,
    openai_ocr_model: render_provider_summary,
    ocr_mode: render_provider_summary
});

function render_provider_summary(frm) {
    const field = frm.fields_dict.provider_guide_html;
    if (!field || !field.$wrapper) {
        return;
    }

    const escape = (value) => $('<div>').text(value || '').html();
    const endpoint = (value, fallback) => escape(value || fallback);
    const chatIsOpenAI = frm.doc.chat_provider === 'OpenAI';
    const chatIsOllama = frm.doc.chat_provider === 'Ollama';
    const chatIsLMStudio = frm.doc.chat_provider === 'LM Studio';
    const embeddingsAreLocal =
        frm.doc.embedding_provider === 'Local (sentence-transformers)';
    const embeddingsIsOpenAI = frm.doc.embedding_provider === 'OpenAI';
    const embeddingsUseLMStudio = frm.doc.embedding_provider === 'LM Studio';

    let chatDetail;
    if (chatIsOllama) {
        chatDetail = `${__('Ollama server')}: ${endpoint(
            frm.doc.chat_endpoint,
            'http://localhost:11434'
        )}. ${__('No API key is used for chat.')}`;
    } else if (chatIsLMStudio) {
        chatDetail = `${__('LM Studio OpenAI-compatible server')}: ${endpoint(
            frm.doc.chat_endpoint,
            'http://localhost:1234/v1'
        )}. ${__(
            'No API key is required.'
        )}`;
    } else if (chatIsOpenAI) {
        chatDetail = `${__('Official OpenAI API')}. ${__('Uses the OpenAI API Key below.')}`;
    } else {
        chatDetail = `${__('OpenAI-compatible API')}: ${endpoint(
            frm.doc.chat_endpoint,
            __('endpoint required')
        )}. ${__('Uses the OpenAI Compatible API Key below when authentication is required.')}`;
    }

    let embeddingDetail;
    if (embeddingsAreLocal) {
        embeddingDetail = __(
            'Runs Sentence Transformers inside the worker.'
        );
    } else if (embeddingsUseLMStudio) {
        embeddingDetail = `${__('LM Studio embeddings API')}: ${endpoint(
            frm.doc.embedding_endpoint,
            'http://localhost:1234/v1'
        )}. ${__(
            'Requires an embedding model loaded in LM Studio.'
        )}`;
    } else if (embeddingsIsOpenAI) {
        embeddingDetail = `${__('Official OpenAI embeddings API')}. ${__('Uses the OpenAI API Key below.')}`;
    } else {
        embeddingDetail = `${__('OpenAI-compatible embeddings API')}: ${endpoint(
            frm.doc.embedding_endpoint,
            __('endpoint required')
        )}. ${__('Uses the OpenAI Compatible API Key below.')}`;
    }

    const ocrDetail = frm.doc.enable_openai_for_ocr
        ? __(
            'OCRmyPDF runs locally first. OpenAI Vision is called only for pages that still have no extracted text.'
        )
        : __('OCR is local only. OpenAI Vision fallback is disabled.');

    const card = (title, provider, model, detail, tone) => `
        <div style="
            border: 1px solid var(--border-color);
            border-left: 4px solid ${tone};
            border-radius: 8px;
            padding: 12px 14px;
            background: var(--card-bg);
        ">
            <div style="font-size: 11px; color: var(--text-muted);">${title}</div>
            <div style="font-weight: 600; margin: 3px 0;">
                ${escape(provider)}
                <span style="font-weight: 400; color: var(--text-muted);">
                    &middot; ${escape(model)}
                </span>
            </div>
            <div style="font-size: 12px; line-height: 1.45;">${detail}</div>
        </div>
    `;

    field.$wrapper.html(`
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px; margin-bottom: 14px;">
            ${card(
                __('Document Chat'),
                frm.doc.chat_provider || 'OpenAI',
                frm.doc.rag_model || 'gpt-5-mini',
                chatDetail,
                (chatIsOllama || chatIsLMStudio) ? '#16a34a' : '#2563eb'
            )}
            ${card(
                __('Semantic Search'),
                frm.doc.embedding_provider || 'Local (sentence-transformers)',
                frm.doc.embedding_model || 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
                embeddingDetail,
                (embeddingsAreLocal || embeddingsUseLMStudio)
                    ? '#16a34a'
                    : '#2563eb'
            )}
            ${card(
                __('OCR'),
                frm.doc.enable_openai_for_ocr
                    ? __('Local OCR + OpenAI fallback')
                    : __('Local OCR only'),
                frm.doc.enable_openai_for_ocr
                    ? (frm.doc.openai_ocr_model || 'gpt-5-mini')
                    : (frm.doc.ocr_mode || 'Auto'),
                ocrDetail,
                frm.doc.enable_openai_for_ocr ? '#d97706' : '#16a34a'
            )}
        </div>
        ${(chatIsLMStudio || embeddingsUseLMStudio) ? `
            <div class="alert alert-warning" style="margin-bottom: 14px;">
                ${__(
                    'LM Studio must be reachable from the Frappe worker. localhost refers to the Frappe server, not the browser computer.'
                )}
            </div>
        ` : ''}
    `);
}
