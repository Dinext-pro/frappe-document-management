frappe.pages['indexed-doctypes-search'].on_page_load = function(wrapper) {
    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: __('Indexed DocTypes Search'),
        single_column: true
    });
    $(frappe.render_template('indexed_doctypes_search', {})).appendTo(page.main);
    wrapper.indexed_search_controller = new IndexedDocTypesSearchController(wrapper, page);
};

frappe.pages['indexed-doctypes-search'].on_page_hide = function(wrapper) {
    wrapper.indexed_search_controller?.onPageHide();
};

frappe.pages['indexed-doctypes-search'].on_page_show = function(wrapper) {
    wrapper.indexed_search_controller?.onPageShow();
};

const RECENT_SEARCHES_KEY = 'dm_indexed_search_recent';
const MAX_RECENT_SEARCHES = 8;
const INDEX_REBUILT_EVENT = 'document_management:indexed_search_rebuilt';

class IndexedDocTypesSearchController {
    constructor(wrapper, page) {
        this.wrapper = $(wrapper);
        this.page = page;
        this.selectedTypes = new Set();
        this.options = [];
        this.facets = {};
        this.lastAllFacets = {};
        this.facetsTruncated = false;
        this.currentQuery = '';
        this.currentTerms = [];
        this.foldedHighlightTerms = [];
        this.currentPage = 1;
        this.pageLength = 25;
        this.sortBy = 'relevance';
        this.dateFrom = '';
        this.dateTo = '';
        this.activeIndex = -1;
        this.searchRequest = 0;
        this.defaultStatusText = '';
        this.isManager = (frappe.user_roles || []).includes('System Manager');
        this.onIndexRebuilt = () => {
            this.loadOptions();
            frappe.show_alert({
                message: __('Indexed DocTypes Search index rebuilt'),
                indicator: 'green'
            });
            if (this.currentQuery) {
                this.search(this.currentPage, false);
            }
        };
        this.bind();
        this.setupActions();
        this.bindRealtime();
        this.renderRecentSearches();
        this.loadOptions().then(() => this.restoreFromUrl());
    }

    onPageHide() {
        this.unbindGlobalShortcuts();
        this.unbindRealtime();
    }

    onPageShow() {
        this.bindGlobalShortcuts();
        this.bindRealtime();
    }

    bindRealtime() {
        if (frappe.realtime && frappe.realtime.on) {
            frappe.realtime.off?.(INDEX_REBUILT_EVENT, this.onIndexRebuilt);
            frappe.realtime.on(INDEX_REBUILT_EVENT, this.onIndexRebuilt);
        }
    }

    unbindRealtime() {
        if (frappe.realtime && frappe.realtime.off) {
            frappe.realtime.off(INDEX_REBUILT_EVENT, this.onIndexRebuilt);
        }
    }

    restoreFromUrl() {
        const params = new URLSearchParams(window.location.search);
        const query = (params.get('q') || '').trim();
        const page = Math.max(parseInt(params.get('page'), 10) || 1, 1);
        const sort = params.get('sort') || 'relevance';
        const from = params.get('from') || '';
        const to = params.get('to') || '';
        const limit = parseInt(params.get('limit'), 10) || 25;
        const allowedOptionValues = new Set(this.options.map((opt) => opt.value));
        const doctypes = (params.get('doctypes') || '')
            .split(',')
            .map((v) => v.trim())
            .filter((v) => v && allowedOptionValues.has(v));

        if (['relevance', 'modified_desc', 'modified_asc', 'title_asc'].includes(sort)) {
            this.sortBy = sort;
            this.wrapper.find('#indexed-doctypes-search-sort').val(sort);
        }
        if ([25, 50, 100].includes(limit)) {
            this.pageLength = limit;
            this.wrapper.find('#indexed-doctypes-search-limit').val(String(limit));
        }
        this.dateFrom = from;
        this.dateTo = to;
        this.wrapper.find('#indexed-doctypes-search-from').val(from);
        this.wrapper.find('#indexed-doctypes-search-to').val(to);

        this.selectedTypes = new Set(doctypes);
        this.renderTypes();
        this.updateFilterUiState();

        if (query) {
            this.wrapper.find('#indexed-doctypes-search-input').val(query);
            this.toggleClearButton();
            this.search(page, false);
        }
    }

    syncUrl(query, page) {
        if (!window.history || !window.history.replaceState) return;
        const url = new URL(window.location.href);
        const setOrDel = (key, val) => {
            if (val) url.searchParams.set(key, val);
            else url.searchParams.delete(key);
        };
        setOrDel('q', query);
        setOrDel('page', page > 1 ? String(page) : '');
        setOrDel('doctypes', this.selectedTypes.size ? Array.from(this.selectedTypes).join(',') : '');
        setOrDel('sort', this.sortBy !== 'relevance' ? this.sortBy : '');
        setOrDel('from', this.dateFrom);
        setOrDel('to', this.dateTo);
        setOrDel('limit', this.pageLength !== 25 ? String(this.pageLength) : '');
        window.history.replaceState({}, '', url.toString());
    }

    setupActions() {
        if (this.isManager) {
            this.page.add_menu_item(__('Index Status'), () => this.showIndexStatus());
            this.page.add_menu_item(__('Rebuild Indexed DocTypes Search Index'), () => {
                this.confirmAndQueueRebuild();
            });
        }
    }

    confirmAndQueueRebuild() {
        frappe.confirm(
            __('Rebuild all configured search indexes?'),
            () => this.call('enqueue_rebuild_index').then(() => {
                frappe.show_alert({
                    message: __('Search index rebuild queued'),
                    indicator: 'blue'
                });
            })
        );
    }

    showIndexStatus() {
        this.call('get_index_status').then((status) => {
            const escape = (val) => $('<div>').text(val == null ? '' : String(val)).html();
            const badge = status.ready
                ? `<span class="indicator-pill green">${__('Ready')}</span>`
                : `<span class="indicator-pill red">${__('Rebuild Required')}</span>`;
            const byType = Object.entries(status.by_doctype || {})
                .map(([dt, count]) => `<tr><td>${escape(dt)}</td><td style="text-align:right;">${escape(count)}</td></tr>`)
                .join('');
            const rebuiltAt = status.rebuilt_at
                ? frappe.datetime.str_to_user(status.rebuilt_at)
                : __('Never');
            frappe.msgprint({
                title: __('Indexed DocTypes Search Status'),
                wide: true,
                message: `
                    <p>
                        <strong>${__('Status')}:</strong> ${badge} &middot;
                        <strong>${__('Total Indexed')}:</strong> ${escape(status.num_docs || 0)} &middot;
                        <strong>${__('Last Rebuilt')}:</strong> ${escape(rebuiltAt)}
                    </p>
                    ${status.generation ? `<p class="text-muted" style="font-size:12px;">${__('Generation')}: <code>${escape(status.generation)}</code> (v${escape(status.index_version)})</p>` : ''}
                    ${status.error ? `<div class="alert alert-warning">${escape(status.error)}</div>` : ''}
                    ${byType
                        ? `<table class="table table-bordered table-condensed"><thead><tr><th>${__('DocType')}</th><th style="text-align:right;">${__('Indexed Records')}</th></tr></thead><tbody>${byType}</tbody></table>`
                        : `<p class="text-muted">${__('No DocTypes configured for indexing.')}</p>`}
                `
            });
        });
    }

    bind() {
        const $input = this.wrapper.find('#indexed-doctypes-search-input');
        this.wrapper.find('#indexed-doctypes-search-button').on('click', () => {
            this.search(1);
        });
        this.wrapper.find('#indexed-doctypes-search-clear').on('click', () => {
            this.clearSearch();
            $input.trigger('focus');
        });
        this.wrapper.find('#indexed-doctypes-search-help').on('click', () => {
            this.showSyntaxHelp();
        });
        $input.on('input', () => this.toggleClearButton());
        $input.on('keydown', (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                this.search(1);
            } else if (event.key === 'Escape') {
                event.preventDefault();
                this.clearSearch();
            } else if (event.key === 'ArrowDown') {
                const cards = this.resultCards();
                if (cards.length) {
                    event.preventDefault();
                    $input.trigger('blur');
                    this.setActiveResult(0);
                }
            }
        });

        this.wrapper.find('#indexed-doctypes-search-sort').on('change', (event) => {
            this.sortBy = event.currentTarget.value || 'relevance';
            this.updateFilterUiState();
            if (this.currentQuery) this.search(1);
            else this.syncUrl('', 1);
        });
        this.wrapper.find('#indexed-doctypes-search-from').on('change', (event) => {
            this.dateFrom = event.currentTarget.value || '';
            this.updateFilterUiState();
            if (this.currentQuery) this.search(1);
            else this.syncUrl('', 1);
        });
        this.wrapper.find('#indexed-doctypes-search-to').on('change', (event) => {
            this.dateTo = event.currentTarget.value || '';
            this.updateFilterUiState();
            if (this.currentQuery) this.search(1);
            else this.syncUrl('', 1);
        });
        this.wrapper.find('#indexed-doctypes-search-limit').on('change', (event) => {
            this.pageLength = parseInt(event.currentTarget.value, 10) || 25;
            this.updateFilterUiState();
            if (this.currentQuery) this.search(1);
            else this.syncUrl('', 1);
        });
        this.wrapper.find('#indexed-doctypes-reset-filters').on('click', () => {
            this.selectedTypes.clear();
            this.sortBy = 'relevance';
            this.dateFrom = '';
            this.dateTo = '';
            this.pageLength = 25;
            this.wrapper.find('#indexed-doctypes-search-sort').val('relevance');
            this.wrapper.find('#indexed-doctypes-search-from').val('');
            this.wrapper.find('#indexed-doctypes-search-to').val('');
            this.wrapper.find('#indexed-doctypes-search-limit').val('25');
            this.renderTypes();
            this.updateFilterUiState();
            if (this.currentQuery) this.search(1);
            else this.syncUrl('', 1);
        });

        this.bindGlobalShortcuts();
    }

    unbindGlobalShortcuts() {
        $(document).off('keydown.indexed_doctypes_search');
    }

    bindGlobalShortcuts() {
        $(document).off('keydown.indexed_doctypes_search').on('keydown.indexed_doctypes_search', (event) => {
            const input = this.wrapper.find('#indexed-doctypes-search-input');
            if (!input.is(':visible')) return;
            const tag = (document.activeElement?.tagName || '').toLowerCase();
            const isTyping = ['input', 'textarea', 'select'].includes(tag) || document.activeElement?.isContentEditable;
            const hasOpenModal = $('.modal.show, .modal.in').length > 0;
            if (hasOpenModal) return;

            if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey && !isTyping) {
                event.preventDefault();
                input.trigger('focus');
                return;
            }
            if (isTyping) return;

            const cards = this.resultCards();
            if (!cards.length) return;
            if (event.key === 'ArrowDown') {
                event.preventDefault();
                this.setActiveResult(Math.min(this.activeIndex + 1, cards.length - 1));
            } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                if (this.activeIndex <= 0) {
                    this.setActiveResult(-1);
                    input.trigger('focus');
                } else {
                    this.setActiveResult(this.activeIndex - 1);
                }
            } else if (event.key === 'Enter' && this.activeIndex >= 0 && cards[this.activeIndex]) {
                event.preventDefault();
                const $card = $(cards[this.activeIndex]);
                const href = $card.data('route');
                const rowData = $card.data('searchRow');
                if (event.ctrlKey || event.metaKey) {
                    if (href) window.open(href, '_blank', 'noopener');
                } else if (rowData) {
                    this.openResultModal(rowData);
                } else if (href) {
                    window.location.href = href;
                }
            } else if (event.key === 'Escape') {
                event.preventDefault();
                this.clearSearch();
            }
        });
    }

    resultCards() {
        return this.wrapper.find('#indexed-doctypes-search-results .search-result');
    }

    showSyntaxHelp() {
        frappe.msgprint({
            title: __('Search Syntax & Shortcuts'),
            message: `
                <table class="table table-bordered table-condensed">
                    <tbody>
                        <tr><td><code>"Juan Perez"</code></td><td>${__('Match an exact phrase across title, ID, content, or comments')}</td></tr>
                        <tr><td><code>factura -anulado</code></td><td>${__('Exclude records containing a specific term')}</td></tr>
                        <tr><td><code>INV-2026</code></td><td>${__('Prefix matching on record IDs and titles')}</td></tr>
                        <tr><td><code>fatura</code></td><td>${__('Automatic typo-tolerant fallback when no exact matches exist')}</td></tr>
                    </tbody>
                </table>
                <p class="text-muted" style="font-size:12px;margin:8px 0 0;">
                    <strong>${__('Keyboard shortcuts')}:</strong>
                    <code>/</code> ${__('focus search')} &middot;
                    <code>&uarr;/&darr;</code> ${__('navigate results')} &middot;
                    <code>Enter</code> ${__('preview record')} &middot;
                    <code>Ctrl+Enter</code> ${__('open in new tab')} &middot;
                    <code>Esc</code> ${__('clear search')}
                </p>
            `
        });
    }

    setActiveResult(index) {
        const cards = this.resultCards();
        cards.removeClass('keyboard-active');
        this.activeIndex = index;
        if (index >= 0 && cards[index]) {
            const $card = $(cards[index]);
            $card.addClass('keyboard-active');
            cards[index].focus({preventScroll: true});
            cards[index].scrollIntoView({block: 'nearest', behavior: 'smooth'});
        }
    }

    toggleClearButton() {
        const hasVal = Boolean(this.wrapper.find('#indexed-doctypes-search-input').val());
        this.wrapper.find('#indexed-doctypes-search-clear').toggleClass('hidden', !hasVal);
    }

    updateFilterUiState() {
        const hasFilters = Boolean(
            this.selectedTypes.size
            || this.sortBy !== 'relevance'
            || this.dateFrom
            || this.dateTo
            || this.pageLength !== 25
        );
        this.wrapper.find('#indexed-doctypes-reset-filters').toggleClass('hidden', !hasFilters);
    }

    clearSearch() {
        this.searchRequest += 1;
        this.currentQuery = '';
        this.currentTerms = [];
        this.foldedHighlightTerms = [];
        this.currentPage = 1;
        this.facets = {};
        this.lastAllFacets = {};
        this.facetsTruncated = false;
        this.activeIndex = -1;
        this.wrapper.find('#indexed-doctypes-search-input').val('');
        this.toggleClearButton();
        this.renderTypes();
        this.syncUrl('', 1);
        this.wrapper.find('#indexed-doctypes-search-status')
            .removeClass('text-danger')
            .text(this.defaultStatusText);
        this.wrapper.find('#indexed-doctypes-search-results').html(
            `<div class="search-empty">` +
            `<i class="fa fa-search"></i>` +
            `<strong>${__('Enter a query to search the configured indexes.')}</strong>` +
            `<span class="search-shortcuts-hint">${__('Tip: Press / to focus, Up/Down to navigate results, Enter to open, Esc to clear.')}</span>` +
            `</div>`
        );
        this.renderRecentSearches();
    }

    getRecentSearches() {
        try {
            const parsed = JSON.parse(window.localStorage.getItem(RECENT_SEARCHES_KEY) || '[]');
            return Array.isArray(parsed) ? parsed.filter((v) => typeof v === 'string' && v.trim()) : [];
        } catch (e) {
            return [];
        }
    }

    recordRecentSearch(query) {
        const cleaned = (query || '').trim();
        if (!cleaned) return;
        try {
            const existing = this.getRecentSearches().filter((item) => item.toLowerCase() !== cleaned.toLowerCase());
            existing.unshift(cleaned);
            window.localStorage.setItem(
                RECENT_SEARCHES_KEY,
                JSON.stringify(existing.slice(0, MAX_RECENT_SEARCHES))
            );
        } catch (e) {
            // Ignore storage errors (private browsing / quota)
        }
    }

    renderRecentSearches() {
        const container = this.wrapper.find('#indexed-doctypes-recent-searches');
        const items = this.getRecentSearches();
        if (!items.length || this.currentQuery) {
            container.addClass('hidden').empty();
            return;
        }
        container.removeClass('hidden').empty();
        container.append($('<span class="recent-searches-label"></span>').text(__('Recent:')));
        items.forEach((query) => {
            const chip = $('<button type="button" class="recent-search-chip"></button>')
                .text(query)
                .on('click', () => {
                    this.wrapper.find('#indexed-doctypes-search-input').val(query);
                    this.toggleClearButton();
                    this.search(1);
                });
            container.append(chip);
        });
        const clearBtn = $('<button type="button" class="recent-searches-clear"></button>')
            .text(__('Clear history'))
            .on('click', () => {
                try {
                    window.localStorage.removeItem(RECENT_SEARCHES_KEY);
                } catch (e) {}
                this.renderRecentSearches();
            });
        container.append(clearBtn);
    }

    call(method, args = {}) {
        return new Promise((resolve, reject) => {
            frappe.call({
                method: `document_management.search.indexer.${method}`,
                args,
                callback: (response) => resolve(response.message),
                error: reject
            });
        });
    }

    async loadOptions() {
        const options = await this.call('get_search_options');
        this.options = options.doctypes || [];
        this.renderTypes();
        const modes = [];
        if (options.full_text_enabled) modes.push(__('Full text'));
        let status = modes.length
            ? (
                options.indexed_count != null
                    ? __('Enabled: {0} ({1} documents indexed)', [modes.join(' + '), options.indexed_count])
                    : __('Enabled: {0}', [modes.join(' + ')])
            )
            : __('No search indexes are enabled.');
        if (options.full_text_enabled && !options.generic_index_ready) {
            status += ` | ${__(
                'The operational record index must be rebuilt before first use.'
            )}`;
        }
        this.defaultStatusText = status;
        if (!this.currentQuery) {
            this.wrapper.find('#indexed-doctypes-search-status').text(status);
        }
    }

    renderTypes() {
        const container = this.wrapper.find('#indexed-doctypes-search-types').empty();
        const all = $('<button type="button" class="search-type active"></button>')
            .text(__('All'))
            .on('click', () => {
                this.selectedTypes.clear();
                this.renderTypes();
                this.updateFilterUiState();
                if (this.currentQuery) this.search(1);
                else this.syncUrl('', 1);
            });
        container.append(all);
        this.options.forEach((option) => {
            const liveCount = this.facets[option.value];
            const cachedCount = this.lastAllFacets[option.value];
            const isStale = liveCount == null && cachedCount != null && this.selectedTypes.size > 0;
            const count = liveCount != null ? liveCount : (isStale ? cachedCount : null);
            const button = $('<button type="button" class="search-type"></button>')
                .toggleClass('active', this.selectedTypes.has(option.value))
                .on('click', () => {
                    if (this.selectedTypes.has(option.value)) {
                        this.selectedTypes.delete(option.value);
                    } else {
                        this.selectedTypes.add(option.value);
                    }
                    this.renderTypes();
                    this.updateFilterUiState();
                    if (this.currentQuery) this.search(1);
                    else this.syncUrl('', 1);
                });
            button.append($('<span></span>').text(option.label));
            if (this.currentQuery && count != null && count > 0) {
                const suffix = this.facetsTruncated ? '+' : '';
                const badge = $('<span class="search-type-count"></span>')
                    .text(`${count}${suffix}`)
                    .toggleClass('stale', isStale);
                button.append(badge);
            }
            container.append(button);
        });
        all.toggleClass('active', this.selectedTypes.size === 0);
    }

    async search(page = 1, updateUrl = true) {
        const query = this.wrapper.find('#indexed-doctypes-search-input').val().trim();
        if (!query) {
            this.clearSearch();
            return;
        }
        const requestId = ++this.searchRequest;
        this.currentPage = Math.max(Number(page) || 1, 1);
        this.currentQuery = query;
        this.activeIndex = -1;
        this.recordRecentSearch(query);
        this.renderRecentSearches();
        this.toggleClearButton();
        if (updateUrl) {
            this.syncUrl(query, this.currentPage);
        }
        const results = this.wrapper.find('#indexed-doctypes-search-results');
        const status = this.wrapper.find('#indexed-doctypes-search-status');
        const button = this.wrapper.find('#indexed-doctypes-search-button');
        button.prop('disabled', true);
        results.html(
            '<div class="search-empty"><i class="fa fa-spinner fa-spin"></i></div>'
        );
        status.removeClass('text-danger').text(__('Searching...'));
        try {
            const response = await this.call('search', {
                query,
                page: this.currentPage,
                page_length: this.pageLength,
                doctypes: JSON.stringify(Array.from(this.selectedTypes)),
                sort_by: this.sortBy,
                date_from: this.dateFrom || undefined,
                date_to: this.dateTo || undefined,
            });
            if (requestId !== this.searchRequest) return;
            this.currentTerms = response.terms || [];
            this.foldedHighlightTerms = this.highlightTerms()
                .map((term) => this.foldText(term))
                .filter(Boolean);
            this.facets = response.facets || {};
            this.facetsTruncated = Boolean(response.facets_truncated);
            if (this.selectedTypes.size === 0) {
                this.lastAllFacets = {...this.facets};
            }
            this.renderTypes();
            this.renderResults(response);
            const count = (response.exact || []).length;
            const pagination = response.pagination || {};
            status.text(
                response.exact_error
                    ? response.exact_error
                    : count
                        ? __('Results {0}-{1}', [
                            pagination.from || 1,
                            pagination.to || count
                        ])
                        : __('0 results')
            ).toggleClass(
                'text-danger',
                Boolean(response.exact_error)
            );
        } catch (error) {
            if (requestId !== this.searchRequest) return;
            results.html(
                `<div class="search-empty"><strong>${__(
                    'Search is temporarily unavailable.'
                )}</strong></div>`
            );
            status.empty();
        } finally {
            if (requestId === this.searchRequest) {
                button.prop('disabled', false);
            }
        }
    }

    renderResults(response) {
        const container = this.wrapper.find('#indexed-doctypes-search-results').empty();
        const rows = response.exact || [];
        if (response.exact_error) {
            const banner = $('<div class="alert alert-warning search-rebuild-banner"></div>');
            banner.append($('<span></span>').text(response.exact_error));
            if (response.exact_rebuild_required && this.isManager) {
                const btn = $('<button type="button" class="btn btn-xs btn-primary"></button>')
                    .text(__('Rebuild Index Now'))
                    .on('click', () => this.confirmAndQueueRebuild());
                banner.append(btn);
            }
            container.append(banner);
        }
        if (response.fuzzy_fallback && rows.length) {
            container.append(
                $('<div class="search-fuzzy-banner"></div>').text(
                    __('No exact matches found. Showing similar results.')
                )
            );
        }
        this.renderSection(
            container,
            __('Indexed Records'),
            rows
        );
        if (!rows.length && !response.exact_error) {
            container.append(
                `<div class="search-empty"><i class="fa fa-search"></i><strong>${
                    __('No permitted results found.')
                }</strong></div>`
            );
        }
        this.renderPagination(container, response.pagination || {});
    }

    renderPagination(container, pagination) {
        if (!pagination.has_previous && !pagination.has_more) return;
        const controls = $('<nav class="search-pagination"></nav>');
        const previous = $('<button type="button" class="btn btn-default btn-sm"></button>')
            .text(__('Previous'))
            .prop('disabled', !pagination.has_previous)
            .on('click', () => this.search(this.currentPage - 1));
        const page = $('<span class="search-pagination-page"></span>').text(
            __('Page {0}', [pagination.page || this.currentPage])
        );
        const next = $('<button type="button" class="btn btn-default btn-sm"></button>')
            .text(__('Next'))
            .prop('disabled', !pagination.has_more)
            .on('click', () => this.search(this.currentPage + 1));
        controls.append(previous, page, next);
        container.append(controls);
    }

    formatModified(modified) {
        if (!modified) return '';
        try {
            if (frappe.datetime && typeof frappe.datetime.str_to_user === 'function') {
                return frappe.datetime.str_to_user(modified);
            }
        } catch (e) {}
        return String(modified).slice(0, 16);
    }

    renderSection(container, title, rows) {
        if (!rows.length) return;
        const section = $('<section class="result-section"></section>');
        section.append($('<h5 class="result-section-title"></h5>').text(title));
        const list = $('<div class="result-list"></div>');
        rows.forEach((row) => {
            const card = $('<div class="search-result" role="button" tabindex="0"></div>')
                .data('route', row.route)
                .data('searchRow', row)
                .on('click', (event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    if (event.ctrlKey || event.metaKey) {
                        if (row.route) window.open(row.route, '_blank', 'noopener');
                        return;
                    }
                    if (event.shiftKey || event.altKey || event.button !== 0) {
                        return;
                    }
                    this.openResultModal(row);
                })
                .on('auxclick', (event) => {
                    if (event.button === 1 && row.route) {
                        event.preventDefault();
                        event.stopPropagation();
                        window.open(row.route, '_blank', 'noopener');
                    }
                });
            const header = $('<div class="result-header"></div>');
            header.append(
                $('<div class="result-title"></div>').append(
                    this.highlightText(row.title)
                )
            );
            const matchBadge = $('<div class="result-match"></div>')
                .text(this.matchLabel(row));
            if (row.match_type === 'exact') matchBadge.addClass('match-exact');
            else if (row.match_type === 'fuzzy') matchBadge.addClass('match-fuzzy');
            header.append(matchBadge);
            card.append(header);

            const modifiedStr = this.formatModified(row.modified);
            const meta = $('<div class="result-meta"></div>');
            meta.append(document.createTextNode(`${row.doc_type} · `));
            meta.append(this.highlightText(row.doc_name));
            if (modifiedStr) {
                meta.append(document.createTextNode(` · ${__('Modified')}: ${modifiedStr}`));
            }
            card.append(meta);

            if (row.excerpt) {
                card.append(
                    $('<div class="result-excerpt"></div>').append(
                        this.highlightText(row.excerpt)
                    )
                );
            }
            list.append(card);
        });
        section.append(list);
        container.append(section);
    }

    openResultModal(row) {
        if (!row || !row.doc_type || !row.doc_name) return;
        const route = row.route || '';
        const dialog = new frappe.ui.Dialog({
            title: `${row.doc_type}: ${row.title || row.doc_name}`,
            size: 'extra-large',
            primary_action_label: __('Open Record'),
            primary_action: () => {
                dialog.hide();
                this.navigateToRecord(row.form_doctype || row.doc_type, row.form_name || row.doc_name, route, row.child_row);
            },
            secondary_action_label: __('Open in New Tab'),
            secondary_action: () => {
                if (route) window.open(route, '_blank', 'noopener');
            }
        });
        dialog.$wrapper.addClass('dm-search-preview-dialog');
        dialog.$body.html(
            '<div class="search-modal-loading"><i class="fa fa-spinner fa-spin"></i> ' +
            `${__('Loading record preview...')}</div>`
        );
        dialog.show();

        this.call('get_search_result_preview', {
            doc_type: row.doc_type,
            doc_name: row.doc_name,
            query: this.currentQuery || ''
        }).then((preview) => {
            if (!preview) {
                dialog.$body.html(
                    `<div class="search-modal-empty">${__('Unable to load record preview.')}</div>`
                );
                return;
            }
            this.renderPreviewModal(dialog, preview);
        }).catch(() => {
            dialog.$body.html(
                `<div class="search-modal-empty">${__('Unable to load record preview.')}</div>`
            );
        });
    }

    buildRecordUrl(route, childRow) {
        if (!route) return '';
        if (!childRow || !childRow.table_fieldname) return route;
        const params = new URLSearchParams();
        params.set('dm_child_field', childRow.table_fieldname);
        if (childRow.row_name) params.set('dm_child_row', childRow.row_name);
        if (childRow.idx) params.set('dm_child_idx', String(childRow.idx));
        const sep = route.includes('?') ? '&' : '?';
        return `${route}${sep}${params.toString()}`;
    }

    openChildTableRow(frm, tableFieldname, rowName, idx) {
        const helpers = frappe.document_management && frappe.document_management.helpers;
        if (helpers && typeof helpers.open_child_table_row === 'function') {
            return helpers.open_child_table_row(frm, tableFieldname, rowName, idx);
        }
        if (!frm || !tableFieldname || !frm.fields_dict || !frm.fields_dict[tableFieldname]) {
            return false;
        }
        const field = frm.fields_dict[tableFieldname];
        const grid = field && field.grid;
        if (!grid) return false;

        if (field.section && typeof field.section.is_collapsed === 'function' && field.section.is_collapsed()) {
            try {
                field.section.collapse(false);
            } catch (e) {}
        }
        if (typeof frm.scroll_to_field === 'function') {
            try {
                frm.scroll_to_field(tableFieldname);
            } catch (e) {}
        }

        const numericIdx = parseInt(idx, 10) || 0;
        const pagination = grid.grid_pagination;
        if (pagination && numericIdx > 0) {
            const pageLength = Number(pagination.page_length) || 50;
            const targetPage = Math.ceil(numericIdx / pageLength);
            const currentPage = Number(pagination.page_index) || 1;
            if (targetPage !== currentPage) {
                if (typeof pagination.go_to_page === 'function') {
                    pagination.go_to_page(targetPage);
                } else {
                    pagination.page_index = targetPage;
                    if (typeof grid.refresh === 'function') grid.refresh();
                }
            }
        }

        let gridRow = null;
        if (rowName && typeof grid.get_row === 'function') {
            try {
                gridRow = grid.get_row(rowName);
            } catch (e) {}
        }
        if (!gridRow && rowName && grid.grid_rows_by_docname) {
            gridRow = grid.grid_rows_by_docname[rowName] || null;
        }
        if (!gridRow && Array.isArray(grid.grid_rows)) {
            gridRow = grid.grid_rows.find((row) => (
                row && row.doc && (
                    (rowName && String(row.doc.name) === String(rowName))
                    || (numericIdx > 0 && Number(row.doc.idx) === numericIdx)
                )
            )) || null;
        }
        if (!gridRow) return false;

        const alreadyOpen = Boolean(
            (grid.open_grid_row && grid.open_grid_row === gridRow)
            || (gridRow.wrapper && gridRow.wrapper.hasClass && gridRow.wrapper.hasClass('grid-row-open'))
        );
        if (!alreadyOpen) {
            if (typeof gridRow.toggle_view === 'function') {
                gridRow.toggle_view(true);
            } else if (typeof gridRow.show_form === 'function') {
                gridRow.show_form();
            } else if (gridRow.open_form_button && typeof gridRow.open_form_button.trigger === 'function') {
                gridRow.open_form_button.trigger('click');
            }
            const nowOpen = Boolean(
                (grid.open_grid_row && grid.open_grid_row === gridRow)
                || (gridRow.wrapper && gridRow.wrapper.hasClass && gridRow.wrapper.hasClass('grid-row-open'))
            );
            if (!nowOpen && typeof gridRow.show_form === 'function') {
                gridRow.show_form();
            }
        }
        const rowEl = gridRow.wrapper && (gridRow.wrapper[0] || gridRow.wrapper);
        if (rowEl && typeof rowEl.scrollIntoView === 'function') {
            try {
                rowEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
            } catch (e) {}
        }
        return true;
    }

    focusChildRowOnForm(docType, docName, tableFieldname, rowName, idx, maxAttempts = 45) {
        if (!tableFieldname) return;
        const helpers = frappe.document_management && frappe.document_management.helpers;
        if (helpers && typeof helpers.focus_child_row_on_form === 'function') {
            helpers.focus_child_row_on_form(docType, docName, tableFieldname, rowName, idx, maxAttempts);
            return;
        }
        let attempts = 0;
        let openedOnce = false;
        const resolveFrm = () => (
            window.cur_frm
            || (frappe.container && frappe.container.page && frappe.container.page.frm)
            || null
        );
        const recheck = () => {
            const liveFrm = resolveFrm();
            if (
                liveFrm
                && liveFrm.doc
                && (!docType || String(liveFrm.doctype) === String(docType))
                && (!docName || String(liveFrm.docname) === String(docName))
            ) {
                this.openChildTableRow(liveFrm, tableFieldname, rowName, idx);
            }
        };
        const tryOpen = () => {
            attempts += 1;
            const frm = resolveFrm();
            const matchesDoc = frm
                && frm.doc
                && (!docType || String(frm.doctype) === String(docType))
                && (!docName || String(frm.docname) === String(docName));
            if (matchesDoc && this.openChildTableRow(frm, tableFieldname, rowName, idx)) {
                if (!openedOnce) {
                    openedOnce = true;
                    window.setTimeout(recheck, 400);
                    window.setTimeout(recheck, 950);
                }
                return;
            }
            if (attempts < maxAttempts) {
                window.setTimeout(tryOpen, 150);
            }
        };
        window.setTimeout(tryOpen, 150);
    }

    navigateToRecord(docType, docName, route, childRow) {
        if (typeof frappe.set_route === 'function' && docType && docName) {
            frappe.set_route('Form', docType, docName);
            if (childRow && childRow.table_fieldname) {
                this.focusChildRowOnForm(
                    docType,
                    docName,
                    childRow.table_fieldname,
                    childRow.row_name || '',
                    childRow.idx || 0
                );
            }
            return;
        }
        const targetUrl = this.buildRecordUrl(route, childRow);
        if (targetUrl) {
            window.location.href = targetUrl;
        }
    }

    renderPreviewModal(dialog, preview) {
        const matchedRows = preview.matched_child_rows || [];
        const hasChildRows = matchedRows.length > 0;
        const hasParentFields = (preview.fields || []).length > 0;
        const isChildPrimary = preview.match_source === 'child_table' && hasChildRows;
        let activeView = isChildPrimary ? 'child' : 'doctype';
        let activeChildIndex = 0;

        const activeChildRow = () => (
            activeView === 'child' && matchedRows[activeChildIndex]
                ? matchedRows[activeChildIndex]
                : null
        );

        const updateActions = () => {
            const childRow = activeChildRow();
            const label = childRow
                ? __('Open Row in Record')
                : __('Open Record');
            dialog.set_primary_action(label, () => {
                dialog.hide();
                this.navigateToRecord(
                    preview.doc_type,
                    preview.doc_name,
                    preview.route,
                    childRow
                );
            });
            if (preview.route) {
                dialog.set_secondary_action(() => {
                    const url = this.buildRecordUrl(preview.route, childRow);
                    if (url) window.open(url, '_blank', 'noopener');
                });
            }
        };

        const updateTitle = () => {
            const childRow = activeChildRow();
            if (childRow) {
                const rowTitle = `${childRow.table_label} #${childRow.idx || 1} — ${preview.title || preview.doc_name}`;
                dialog.set_title(rowTitle);
            } else {
                dialog.set_title(`${preview.doc_type}: ${preview.title || preview.doc_name}`);
            }
        };

        const renderBody = () => {
            updateTitle();
            updateActions();
            const $wrap = $('<div class="search-preview-modal"></div>');

            // Header metadata bar
            const $meta = $('<div class="search-preview-meta"></div>');
            $meta.append($('<span class="search-preview-pill"></span>').text(preview.doc_type));
            $meta.append($('<span class="search-preview-id"></span>').append(this.highlightText(preview.doc_name)));
            if (preview.status) {
                $meta.append($('<span class="search-preview-status"></span>').text(preview.status));
            }
            const modStr = this.formatModified(preview.modified);
            if (modStr) {
                $meta.append(
                    $('<span class="search-preview-date"></span>').text(`${__('Modified')}: ${modStr}`)
                );
            }
            $wrap.append($meta);

            // View switcher tabs when both child rows and parent fields exist
            if (hasChildRows && hasParentFields) {
                const $tabs = $('<div class="search-preview-tabs"></div>');
                const childTabLabel = matchedRows.length === 1
                    ? __('Matched Child Row')
                    : __('Matched Child Rows ({0})', [matchedRows.length]);
                const $childTab = $('<button type="button" class="search-preview-tab"></button>')
                    .text(childTabLabel)
                    .toggleClass('active', activeView === 'child')
                    .on('click', () => {
                        activeView = 'child';
                        renderBody();
                    });
                const $docTab = $('<button type="button" class="search-preview-tab"></button>')
                    .text(__('Full Record'))
                    .toggleClass('active', activeView === 'doctype')
                    .on('click', () => {
                        activeView = 'doctype';
                        renderBody();
                    });
                if (isChildPrimary) {
                    $tabs.append($childTab, $docTab);
                } else {
                    $tabs.append($docTab, $childTab);
                }
                $wrap.append($tabs);
            }

            if (activeView === 'child' && hasChildRows) {
                if (matchedRows.length > 1) {
                    const $rowNav = $('<div class="search-preview-row-switcher"></div>');
                    matchedRows.forEach((childRow, idx) => {
                        const $btn = $('<button type="button" class="search-preview-row-chip"></button>')
                            .text(`${childRow.table_label} #${childRow.idx || idx + 1}`)
                            .toggleClass('active', idx === activeChildIndex)
                            .on('click', () => {
                                activeChildIndex = idx;
                                renderBody();
                            });
                        $rowNav.append($btn);
                    });
                    $wrap.append($rowNav);
                }

                const currentRow = matchedRows[activeChildIndex] || matchedRows[0];
                const $banner = $('<div class="search-preview-child-banner"></div>');
                $banner.append(
                    $('<strong></strong>').text(
                        __('Child Table Row: {0} (#{1})', [
                            currentRow.table_label || currentRow.child_doctype,
                            currentRow.idx || 1
                        ])
                    )
                );
                if (currentRow.child_doctype && currentRow.child_doctype !== currentRow.table_label) {
                    $banner.append(
                        $('<span class="text-muted"></span>').text(` · ${currentRow.child_doctype}`)
                    );
                }
                $wrap.append($banner);
                $wrap.append(this.buildPreviewFieldsGrid(currentRow.fields || []));
            } else {
                const parentFields = preview.fields || [];
                const matchedFirst = [
                    ...parentFields.filter((f) => f.matched),
                    ...parentFields.filter((f) => !f.matched)
                ];
                $wrap.append(this.buildPreviewFieldsGrid(matchedFirst));

                const comments = preview.matched_comments || [];
                if (comments.length) {
                    const $cSection = $('<div class="search-preview-comments"></div>');
                    $cSection.append(
                        $('<h6 class="search-preview-subtitle"></h6>').text(__('Matching Comments'))
                    );
                    comments.forEach((comment) => {
                        const $item = $('<div class="search-preview-comment"></div>');
                        const $head = $('<div class="search-preview-comment-head"></div>');
                        const created = this.formatModified(comment.creation);
                        $head.text(
                            [comment.owner, created].filter(Boolean).join(' · ')
                        );
                        const $body = $('<div class="search-preview-comment-body"></div>').append(
                            this.highlightText(comment.content)
                        );
                        $item.append($head, $body);
                        $cSection.append($item);
                    });
                    $wrap.append($cSection);
                }
            }

            const attachments = preview.matched_attachments || [];
            if (attachments.length) {
                const section = $('<div class="search-preview-comments"></div>');
                section.append($('<h6 class="search-preview-subtitle"></h6>').text(__('Matching Attachments')));
                attachments.forEach((file) => {
                    const item = $('<div class="search-preview-comment"></div>');
                    item.append($('<a target="_blank" rel="noopener"></a>').attr('href', file.route).text(file.title));
                    item.append($('<div class="search-preview-comment-body"></div>').append(this.highlightText(file.excerpt)));
                    section.append(item);
                });
                $wrap.append(section);
            }
            dialog.$body.empty().append($wrap);
        };

        renderBody();
    }

    buildPreviewFieldsGrid(fields) {
        if (!fields || !fields.length) {
            return $('<div class="search-modal-empty"></div>').text(__('No visible fields to display.'));
        }
        const $grid = $('<div class="search-preview-grid"></div>');
        fields.forEach((field) => {
            const valStr = String(field.value || '');
            const isWide = valStr.length > 90 || ['Text', 'Text Editor', 'Long Text', 'Small Text'].includes(field.fieldtype);
            const $cell = $('<div class="search-preview-field"></div>')
                .toggleClass('is-matched', Boolean(field.matched))
                .toggleClass('is-wide', isWide);
            $cell.append(
                $('<div class="search-preview-field-label"></div>').text(field.label || field.fieldname)
            );
            $cell.append(
                $('<div class="search-preview-field-value"></div>').append(
                    this.highlightText(valStr)
                )
            );
            $grid.append($cell);
        });
        return $grid;
    }

    highlightText(value) {
        const fragment = document.createDocumentFragment();
        const text = String(value || '');
        const terms = this.foldedHighlightTerms && this.foldedHighlightTerms.length
            ? this.foldedHighlightTerms
            : this.highlightTerms().map((term) => this.foldText(term)).filter(Boolean);
        if (!terms.length) {
            fragment.append(document.createTextNode(text));
            return fragment;
        }

        const isAscii = /^[\x00-\x7f]*$/.test(text);
        const folded = isAscii
            ? { text: text.toLowerCase(), offsets: null }
            : this.foldTextWithOffsets(text);
        const ranges = [];
        terms.forEach((term) => {
            let searchFrom = 0;
            while (searchFrom < folded.text.length) {
                const index = folded.text.indexOf(term, searchFrom);
                if (index < 0) break;
                if (folded.offsets) {
                    const first = folded.offsets[index];
                    const last = folded.offsets[index + term.length - 1];
                    if (first && last) {
                        ranges.push([first.start, last.end]);
                    }
                } else {
                    ranges.push([index, index + term.length]);
                }
                searchFrom = index + Math.max(term.length, 1);
            }
        });
        ranges.sort((left, right) => left[0] - right[0] || right[1] - left[1]);
        const merged = [];
        ranges.forEach((range) => {
            const previous = merged[merged.length - 1];
            if (previous && range[0] <= previous[1]) {
                previous[1] = Math.max(previous[1], range[1]);
            } else {
                merged.push(range.slice());
            }
        });

        let lastIndex = 0;
        merged.forEach(([start, end]) => {
            if (start > lastIndex) {
                fragment.append(
                    document.createTextNode(text.slice(lastIndex, start))
                );
            }
            const mark = document.createElement('mark');
            mark.textContent = text.slice(start, end);
            fragment.append(mark);
            lastIndex = end;
        });
        if (lastIndex < text.length) {
            fragment.append(document.createTextNode(text.slice(lastIndex)));
        }
        return fragment;
    }

    matchLabel(row) {
        if (row.match_type === 'exact') {
            return __('Exact match');
        }
        if (row.match_type === 'all_terms') {
            return __('All terms');
        }
        if (row.match_type === 'fuzzy') {
            return __('Similar match');
        }
        const coverage = Math.round(Number(row.coverage || 0) * 100);
        return __('Partial {0}%', [coverage]);
    }

    foldText(value) {
        return String(value || '')
            .normalize('NFKD')
            .replace(/\p{M}/gu, '')
            .toLocaleLowerCase();
    }

    foldTextWithOffsets(value) {
        const source = String(value || '');
        const offsets = [];
        let text = '';
        let sourceIndex = 0;
        for (const character of source) {
            const foldedCharacter = this.foldText(character);
            text += foldedCharacter;
            for (let index = 0; index < foldedCharacter.length; index += 1) {
                offsets.push({
                    start: sourceIndex,
                    end: sourceIndex + character.length
                });
            }
            sourceIndex += character.length;
        }
        return { text, offsets };
    }

    highlightTerms() {
        return Array.from(
            new Set(this.currentTerms)
        ).sort(
            (left, right) => right.length - left.length
        );
    }
}
