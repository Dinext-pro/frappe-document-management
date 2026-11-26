(function(root, factory) {
    if (typeof module === 'object' && module.exports) {
        module.exports = factory();
    } else {
        if (typeof frappe !== 'undefined' && frappe.provide) {
            frappe.provide('frappe.document_management');
            frappe.document_management.helpers = factory();
        }
        root.DocumentManagementHelpers = factory();
    }
}(typeof globalThis !== 'undefined' ? globalThis : this, function() {
    function markdown_anchor_slug(value) {
        return String(value || '')
            .trim()
            .toLowerCase()
            .replace(/[^\p{L}\p{N}\s-]/gu, '')
            .replace(/\s+/g, '-')
            .replace(/-+/g, '-');
    }

    function escape_html(value) {
        return String(value || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    function sanitize_html(html) {
        if (typeof window !== 'undefined' && window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
            return window.DOMPurify.sanitize(html || '');
        }
        if (typeof DOMParser === 'undefined') {
            return escape_html(html || '');
        }
        const parsed_doc = new DOMParser().parseFromString(html || '', 'text/html');
        const blocked = [
            'script', 'style', 'iframe', 'object', 'embed', 'form', 'input',
            'button', 'svg', 'math', 'base', 'meta', 'link', 'template'
        ];
        parsed_doc.querySelectorAll(blocked.join(',')).forEach((node) => node.remove());
        parsed_doc.querySelectorAll('*').forEach((node) => {
            Array.from(node.attributes).forEach((attribute) => {
                const name = attribute.name.toLowerCase();
                const value = attribute.value.replace(/[\u0000-\u001F\u007F\s]+/g, '').toLowerCase();
                if (name.startsWith('on') || name === 'style' || name === 'srcdoc' || name === 'xlink:href') {
                    node.removeAttribute(attribute.name);
                    return;
                }
                if (name === 'href' || name === 'src' || name === 'action' || name === 'formaction') {
                    const allowed = value.startsWith('http://')
                        || value.startsWith('https://')
                        || value.startsWith('mailto:')
                        || value.startsWith('#')
                        || value.startsWith('/');
                    if (!allowed) {
                        node.removeAttribute(attribute.name);
                    }
                }
            });
        });
        return parsed_doc.body.innerHTML;
    }

    function highlight_text(value, terms) {
        const text = String(value || '');
        const unique_terms = Array.from(new Set((terms || []).filter(Boolean)))
            .sort((left, right) => right.length - left.length);
        if (!unique_terms.length) return escape_html(text);
        const escaped_terms = unique_terms.map(
            (term) => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
        );
        const pattern = new RegExp(`(${escaped_terms.join('|')})`, 'giu');
        let html = '';
        let last_index = 0;
        for (const match of text.matchAll(pattern)) {
            html += escape_html(text.slice(last_index, match.index));
            html += `<mark>${escape_html(match[0])}</mark>`;
            last_index = match.index + match[0].length;
        }
        return html + escape_html(text.slice(last_index));
    }

    function get_contrast_color(hex) {
        if (!hex) return '#000000';
        let normalized = String(hex);
        if (normalized.indexOf('#') === 0) normalized = normalized.slice(1);
        if (normalized.length === 3) {
            normalized = normalized[0] + normalized[0] + normalized[1] + normalized[1] + normalized[2] + normalized[2];
        }
        if (normalized.length !== 6) return '#000000';
        const r = parseInt(normalized.slice(0, 2), 16);
        const g = parseInt(normalized.slice(2, 4), 16);
        const b = parseInt(normalized.slice(4, 6), 16);
        const yiq = ((r * 299) + (g * 587) + (b * 114)) / 1000;
        return (yiq >= 128) ? '#000000' : '#ffffff';
    }

    function get_tag_style(color) {
        const tag_color = /^#[0-9a-f]{3}([0-9a-f]{3})?$/i.test(color || '')
            ? color
            : '#e2e8f0';
        return {
            '--tag-color': tag_color,
            '--tag-contrast': get_contrast_color(tag_color)
        };
    }

    function is_pdf(file) {
        return Boolean(file && file.toLowerCase().endsWith('.pdf'));
    }

    function is_img(file) {
        return Boolean(file && file.match(/\.(jpeg|jpg|gif|png|webp)$/i) != null);
    }

    function is_office(file) {
        return Boolean(file && file.match(/\.(doc|docx|ppt|pptx|xls|xlsx)$/i) != null);
    }

    function is_txt(file) {
        return Boolean(file && file.toLowerCase().endsWith('.txt'));
    }

    function is_md(file, doc) {
        if (file && file.toLowerCase().endsWith('.md')) return true;
        if (doc && doc.is_markdown) return true;
        return false;
    }

    function get_file_type(doc) {
        const file_path = doc ? (doc.original_file || doc.document_file) : '';
        if (!file_path) return 'generic';
        if (is_pdf(file_path)) return 'pdf';
        if (is_img(file_path)) return 'image';
        if (is_txt(file_path)) {
            return doc.is_markdown ? 'markdown' : 'text';
        }
        if (is_md(file_path, doc)) return 'markdown';
        if (file_path.match(/\.(doc|docx)$/i)) return 'word';
        if (file_path.match(/\.(xls|xlsx)$/i)) return 'excel';
        if (file_path.match(/\.(ppt|pptx)$/i)) return 'ppt';
        return 'generic';
    }

    function get_icon(doc) {
        return {
            pdf: 'PDF', image: 'IMG', text: 'TXT', markdown: 'MD',
            word: 'DOC', excel: 'XLS', ppt: 'PPT', generic: 'FILE'
        }[get_file_type(doc)];
    }

    function open_child_table_row(frm, table_fieldname, row_name, idx) {
        if (!frm || !table_fieldname || !frm.fields_dict || !frm.fields_dict[table_fieldname]) {
            return false;
        }
        const field = frm.fields_dict[table_fieldname];
        const grid = field && field.grid;
        if (!grid) return false;

        if (typeof frm.scroll_to_field === 'function') {
            try {
                frm.scroll_to_field(table_fieldname);
            } catch (e) {}
        }

        const numericIdx = parseInt(idx, 10) || 0;
        if (grid.grid_pagination && numericIdx > 0) {
            const pageLength = Number(grid.grid_pagination.page_length) || 50;
            const targetPage = Math.ceil(numericIdx / pageLength);
            if (targetPage > 1 && typeof grid.grid_pagination.go_to_page === 'function') {
                grid.grid_pagination.go_to_page(targetPage);
            }
        }

        let gridRow = (row_name && grid.grid_rows_by_docname && grid.grid_rows_by_docname[row_name]) || null;
        if (!gridRow && Array.isArray(grid.grid_rows)) {
            gridRow = grid.grid_rows.find((row) => (
                row && row.doc && (
                    (row_name && row.doc.name === row_name)
                    || (numericIdx > 0 && Number(row.doc.idx) === numericIdx)
                )
            )) || null;
        }
        if (!gridRow) return false;

        if (typeof gridRow.toggle_view === 'function') {
            gridRow.toggle_view(true);
        }
        const rowEl = gridRow.wrapper && (gridRow.wrapper[0] || gridRow.wrapper);
        if (rowEl && typeof rowEl.scrollIntoView === 'function') {
            try {
                rowEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
            } catch (e) {}
        }
        return true;
    }

    function focus_child_row_on_form(doc_type, doc_name, table_fieldname, row_name, idx, maxAttempts = 30) {
        if (typeof window === 'undefined' || !table_fieldname) return;
        let attempts = 0;
        const tryOpen = () => {
            attempts += 1;
            const frm = window.cur_frm;
            const matchesDoc = frm
                && (!doc_type || frm.doctype === doc_type)
                && (!doc_name || frm.docname === doc_name);
            if (matchesDoc && open_child_table_row(frm, table_fieldname, row_name, idx)) {
                return;
            }
            if (attempts < maxAttempts) {
                window.setTimeout(tryOpen, 150);
            }
        };
        window.setTimeout(tryOpen, 120);
    }

    function consume_child_row_url_params() {
        if (typeof window === 'undefined' || !window.location) return;
        const params = new URLSearchParams(window.location.search || '');
        const tableField = params.get('dm_child_field');
        if (!tableField) return;
        const rowName = params.get('dm_child_row') || '';
        const idx = parseInt(params.get('dm_child_idx'), 10) || 0;

        if (window.history && typeof window.history.replaceState === 'function') {
            const cleanUrl = new URL(window.location.href);
            cleanUrl.searchParams.delete('dm_child_field');
            cleanUrl.searchParams.delete('dm_child_row');
            cleanUrl.searchParams.delete('dm_child_idx');
            window.history.replaceState({}, '', cleanUrl.toString());
        }
        focus_child_row_on_form(null, null, tableField, rowName, idx);
    }

    if (typeof window !== 'undefined' && typeof document !== 'undefined') {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', consume_child_row_url_params);
        } else {
            window.setTimeout(consume_child_row_url_params, 100);
        }
        if (typeof frappe !== 'undefined' && frappe.router && typeof frappe.router.on === 'function') {
            frappe.router.on('change', consume_child_row_url_params);
        }
    }

    return {
        markdown_anchor_slug,
        escape_html,
        sanitize_html,
        highlight_text,
        get_contrast_color,
        get_tag_style,
        is_pdf,
        is_img,
        is_office,
        is_txt,
        is_md,
        get_file_type,
        get_icon,
        open_child_table_row,
        focus_child_row_on_form,
    };
}));
