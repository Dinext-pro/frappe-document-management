frappe.pages['document-management-console'].on_page_load = function(wrapper) {
    var page = frappe.ui.make_app_page({
        parent: wrapper,
        title: __('Document Management Console'),
        single_column: true
    });

    wrapper.document_management_console = new DocumentManagementConsole(wrapper, page);
};

frappe.pages['document-management-console'].on_page_show = function(wrapper) {
    wrapper.document_management_console?.on_page_show?.();
};

frappe.pages['document-management-console'].on_page_hide = function(wrapper) {
    wrapper.document_management_console?.on_page_hide?.();
};

function markdown_anchor_slug(value) {
    return String(value || '')
        .trim()
        .toLowerCase()
        .replace(/[^\p{L}\p{N}\s-]/gu, '')
        .replace(/\s+/g, '-')
        .replace(/-+/g, '-');
}

async function upload_document_file(file, method, fields) {
    if (frappe.document_management && frappe.document_management.upload_document_file) {
        return frappe.document_management.upload_document_file(file, method, fields);
    }
    const form_data = new FormData();
    form_data.append('file', file, file.name);
    form_data.append('is_private', '1');
    form_data.append('method', method);
    Object.entries(fields || {}).forEach(([key, value]) => {
        form_data.append(key, value == null ? '' : value);
    });
    const response = await fetch('/api/method/upload_file', {
        method: 'POST',
        body: form_data,
        credentials: 'same-origin',
        headers: {'X-Frappe-CSRF-Token': frappe.csrf_token}
    });
    const payload = await response.json();
    if (!response.ok || payload.exc || !payload.message) {
        throw new Error(payload.exception || __('File upload failed.'));
    }
    return payload.message;
}

class DocumentManagementConsole {
    constructor(wrapper, page) {
        this.wrapper = $(wrapper).find('.layout-main-section');
        this.page = page;
        this.provider = frappe.document_management?.console_adapter;

        const deps = [];
        if (!window.Vue?.compile) {
            deps.push("/assets/document_management/js/vendor/vue.global.prod.js");
        }
        if (!window.DOMPurify) {
            deps.push("/assets/document_management/js/vendor/purify.min.js");
        }
        if (!window.marked) {
            deps.push("/assets/document_management/js/vendor/marked.min.js");
        }
        if (deps.length) {
            frappe.require(deps, () => this.load_pdf_viewer());
        } else {
            this.load_pdf_viewer();
        }
    }

    load_pdf_viewer() {
        const start = () => Promise.resolve(this.provider?.prepare?.()).then(() => this.init_vue()).catch(() => {
            this.wrapper.text(__('The document console could not be loaded.'));
        });
        const viewer_version = "5.7.284-10";
        if (window.LoanManagerPdfSearchViewer?.version === viewer_version) {
            start();
            return;
        }
        frappe.require(
            "/assets/document_management/js/pdf_search_viewer.v5_7_284_10.js",
            start,
        );
    }

    init_vue() {
        const provider = this.provider;
        const template = `
            <div class="paperless-app" :class="{'sidebar-open': selected_doc}">
                <!-- Toolbar -->
                <div class="paperless-toolbar">
                    <div class="search-box">
                        <i class="fa fa-search search-icon"></i>
                        <input type="text" :placeholder="'${__('Search documents...')}'" v-model="filters.search" @input="debounce_fetch" />
                        <button type="button" class="search-help-btn" @click="show_search_help" :title="'${__('Advanced search syntax')}'">
                            <i class="fa fa-question-circle"></i>
                        </button>
                    </div>
                    <div class="filter-box categories-dropdown-container">
                        <button class="categories-dropdown-btn" @click.stop="toggle_category_dropdown">
                            <i class="fa fa-folder-open-o"></i>
                            <span v-if="filters.categories.length === 0">${__('All Categories')}</span>
                            <span v-else>${__('Categories')}: {{ filters.categories.length }}</span>
                            <i class="fa fa-caret-down"></i>
                        </button>
                        <div class="categories-dropdown-menu" v-if="show_category_dropdown" @click.stop>
                            <div class="categories-dropdown-header">
                                <span>${__('Select Categories')}</span>
                                <button v-if="filters.categories.length" class="btn-clear-categories" @click="clear_categories">${__('Clear')}</button>
                            </div>
                            <div class="categories-dropdown-list">
                                <label v-for="cat in categories" :key="cat.name" class="category-filter-item">
                                    <input type="checkbox" :value="cat.name" v-model="filters.categories" @change="fetch_documents(true)" />
                                    <span class="category-name">{{ cat.name }}</span>
                                </label>
                            </div>
                        </div>
                    </div>
                    <component v-if="extensions.status_filter" :is="extensions.status_filter"></component>
                    <div v-if="!provider" class="filter-box statuses-dropdown-container">
                        <button class="statuses-dropdown-btn" @click.stop="toggle_status_dropdown">
                            <i class="fa fa-info-circle"></i>
                            <span v-if="filters.statuses.length === 0">${__('All Statuses')}</span>
                            <span v-else>${__('Statuses')}: {{ filters.statuses.length }}</span>
                            <i class="fa fa-caret-down"></i>
                        </button>
                        <div class="statuses-dropdown-menu" v-if="show_status_dropdown" @click.stop>
                            <div class="statuses-dropdown-header">
                                <span>${__('Select Statuses')}</span>
                                <button v-if="filters.statuses.length" class="btn-clear-statuses" @click="clear_statuses">${__('Clear')}</button>
                            </div>
                            <div class="statuses-dropdown-list">
                                <label class="status-filter-item">
                                    <input type="checkbox" value="Draft" v-model="filters.statuses" @change="fetch_documents(true)" />
                                    <span class="status-name">${__('Draft')}</span>
                                </label>
                                <label class="status-filter-item">
                                    <input type="checkbox" value="Published" v-model="filters.statuses" @change="fetch_documents(true)" />
                                    <span class="status-name">${__('Published')}</span>
                                </label>
                                <label class="status-filter-item">
                                    <input type="checkbox" value="Obsolete" v-model="filters.statuses" @change="fetch_documents(true)" />
                                    <span class="status-name">${__('Obsolete')}</span>
                                </label>
                            </div>
                        </div>
                    </div>
                    <div class="filter-box tags-dropdown-container">
                        <button class="tags-dropdown-btn" @click.stop="toggle_tag_dropdown">
                            <i class="fa fa-tags"></i>
                            <span v-if="filters.tags.length === 0">${__('All Tags')}</span>
                            <span v-else>${__('Tags')}: {{ filters.tags.length }}</span>
                            <i class="fa fa-caret-down"></i>
                        </button>
                        <div class="tags-dropdown-menu" v-if="show_tag_dropdown" @click.stop>
                            <div class="tags-dropdown-header">
                                <span>${__('Select Tags')}</span>
                                <button v-if="filters.tags.length" class="btn-clear-tags" @click="clear_tags">${__('Clear')}</button>
                            </div>
                            <div class="tags-dropdown-list">
                                <label v-for="tag in tags" :key="tag.name" class="tag-filter-item">
                                    <input type="checkbox" :value="tag.name" v-model="filters.tags" @change="fetch_documents(true)" />
                                    <span class="tag-color-indicator" :style="{ backgroundColor: tag.color || '#ccc' }"></span>
                                    <span class="tag-name">{{ tag.name }}</span>
                                </label>
                            </div>
                        </div>
                    </div>
                    <component v-if="extensions.saved_views" :is="extensions.saved_views"></component>
                    <div v-if="!provider" class="filter-box saved-view-box">
                        <select v-model="selected_saved_view" @change="apply_saved_view">
                            <option value="">${__('Saved views')}</option>
                            <option v-for="view in saved_views" :value="view.name">{{ view.view_name }}</option>
                        </select>
                        <button class="btn-refresh" @click="save_view" :title="'${__('Save current view')}'">
                            <i class="fa fa-bookmark-o"></i>
                        </button>
                        <button v-if="selected_saved_view" class="btn-refresh" @click="delete_view" :title="'${__('Delete saved view')}'">
                            <i class="fa fa-times"></i>
                        </button>
                    </div>
                    <div class="filter-box">
                        <select v-model="sort_by" @change="fetch_documents(true)" :aria-label="'${__('Sort')}'" :disabled="sort_disabled" :title="provider && sort_disabled ? '${__('Sorting is fixed in this view')}' : sort_disabled ? '${__('Sorted by search relevance')}' : '${__('Sort')}'">
                            <option value="modified_desc">${__('Sort: Most recent')}</option>
                            <option value="modified_asc">${__('Sort: Oldest')}</option>
                            <option value="title_asc">${__('Sort: Title A-Z')}</option>
                            <option value="title_desc">${__('Sort: Title Z-A')}</option>
                            <option value="document_code_asc">${__('Sort: Code')}</option>
                            <option value="creation_desc">${__('Sort: Created')}</option>
                        </select>
                    </div>
                    <button class="btn-refresh" @click="fetch_documents">
                        <i class="fa fa-refresh"></i>
                    </button>
                    <button v-if="!provider" class="btn-refresh" :class="{active: trash_mode}" @click="toggle_trash">
                        <i class="fa fa-trash"></i> {{ trash_mode ? '${__('Documents')}' : '${__('Trash')}' }}
                    </button>
                    <span class="doc-count" :style="{ visibility: loading ? 'hidden' : 'visible' }">{{ total_documents ?? documents.length }} {{ total_documents == null ? '${__('documents on this page')}' : '${__('documents')}' }}</span>
                    <div class="view-toggles">
                        <button class="btn" :class="{active: view_mode==='grid'}" @click="view_mode='grid'" :title="'${__('Grid View')}'"><i class="fa fa-th"></i></button>
                        <button class="btn" :class="{active: view_mode==='large'}" @click="view_mode='large'" :title="'${__('List Cards')}'"><i class="fa fa-th-list"></i></button>
                        <button class="btn" :class="{active: view_mode==='list'}" @click="view_mode='list'" :title="'${__('Table View')}'"><i class="fa fa-list"></i></button>
                    </div>
                    <component v-if="extensions.toolbar_actions" :is="extensions.toolbar_actions"></component>
                    <button v-if="!provider && !trash_mode" class="btn-upload" @click="show_upload_modal = true" style="margin-left: 10px;">
                        <i class="fa fa-cloud-upload"></i> ${__('Upload')}
                    </button>
                    <button v-if="!provider && !trash_mode && can_purge" class="btn btn-default" @click="import_archive">
                        <i class="fa fa-upload"></i> ${__('Import Archive')}
                    </button>
                </div>

                <div class="bulk-toolbar" v-if="!provider && selected_count">
                    <strong>{{ selected_count }} ${__('selected')}</strong>
                    <button v-if="!trash_mode" class="btn btn-sm btn-default" @click="show_bulk_edit">
                        <i class="fa fa-pencil"></i> ${__('Edit')}
                    </button>
                    <button v-if="!trash_mode" class="btn btn-sm btn-default" @click="export_selected">
                        <i class="fa fa-download"></i> ${__('Export Archive')}
                    </button>
                    <button v-if="!trash_mode" class="btn btn-sm btn-danger" @click="trash_selected">
                        <i class="fa fa-trash"></i> ${__('Move to trash')}
                    </button>
                    <button v-if="trash_mode" class="btn btn-sm btn-primary" @click="restore_selected">
                        <i class="fa fa-undo"></i> ${__('Restore')}
                    </button>
                    <button v-if="trash_mode && can_purge" class="btn btn-sm btn-danger" @click="purge_selected">
                        <i class="fa fa-times"></i> ${__('Delete permanently')}
                    </button>
                    <button class="btn btn-sm btn-default" @click="clear_selection">${__('Clear')}</button>
                </div>

                <div class="paperless-main">
                    <!-- Left: Document List/Grid -->
                    <div class="paperless-content">

                        <component v-if="extensions.list_status" :is="extensions.list_status"></component>
                        <!-- Skeleton Loading -->
                        <div v-if="loading" class="skeleton-grid">
                            <div v-for="n in 8" class="skeleton-card">
                                <div class="skeleton-item skeleton-circle"></div>
                                <div class="skeleton-item skeleton-line w-80"></div>
                                <div class="skeleton-item skeleton-line w-60"></div>
                                <div class="skeleton-item skeleton-line w-40"></div>
                            </div>
                        </div>

                        <!-- Empty State -->
                        <div v-else-if="documents.length === 0" v-show="!hide_empty_state" class="empty-state">
                            <div class="empty-icon"><i class="fa fa-folder-open-o"></i></div>
                            <h5>${__('No documents found')}</h5>
                            <p>${__('Try adjusting your search or filters to find what you are looking for.')}</p>
                        </div>
                        
                        <!-- Grid View (Small Cards) -->
                        <div v-else-if="view_mode === 'grid'" class="paperless-grid">
                            <div v-for="doc in documents" :key="doc.name" 
                                 class="paperless-card" 
                                 :class="{active: selected_doc && selected_doc.name === doc.name, selected: is_selected(doc.name)}"
                                 :data-type="get_file_type(doc)"
                                 @click="select_doc(doc)">
                                <div class="doc-icon-wrapper" :class="get_icon_class(doc)">
                                    <svg class="doc-type-icon" viewBox="0 0 32 38" aria-hidden="true" focusable="false">
                                        <path d="M5 1.5h15l7 7v28H5z M20 1.5v7h7" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" />
                                        <text x="16" y="26" text-anchor="middle">{{ get_icon(doc) }}</text>
                                    </svg>
                                </div>
                                <div class="doc-title">{{ doc.document_name }} <small class="text-muted" style="font-weight: normal;">({{ doc.name }})</small></div>
                                <component v-if="extensions.row_indicator" :is="extensions.row_indicator" :document="doc"></component>
                                <div style="font-size: 10px; color: var(--pc-primary); margin-top: 2px;" v-if="doc.ocr_status === 'Pending' || doc.ocr_status === 'Processing'">
                                    <i class="fa fa-spinner fa-spin"></i> ${__('Processing OCR')}
                                </div>
                                <div class="doc-meta" v-if="doc.document_code">{{ doc.document_code }}</div>
                                <div class="search-excerpt" v-if="filters.search && doc.search_excerpt">
                                    <span v-if="doc.search_page">${__('Page')} {{ doc.search_page }}: </span><span v-html="highlight_text(doc.search_excerpt, doc.search_terms)"></span>
                                </div>
                                <div style="margin-top: 8px; display: flex; gap: 4px; flex-wrap: wrap; justify-content: center;">
                                    <span v-if="doc.party_name" class="keyword-badge" style="font-size: 10px; padding: 1px 6px; margin: 0; background: var(--pc-primary-faded); color: var(--pc-primary-dark); border-color: var(--pc-primary-light);">
                                        <i class="fa fa-handshake-o" style="margin-right: 3px;"></i>{{ doc.party_name }}
                                    </span>
                                    <span v-if="doc.tags" v-for="tag in doc.tags.slice(0, 2)" class="keyword-badge tag-colored" :style="[get_tag_style(tag.color), {fontSize: '10px', padding: '1px 6px', margin: '0'}]">{{ tag.name }}</span>
                                </div>
                            </div>
                        </div>

                        <!-- Large Cards View -->
                        <div v-else-if="view_mode === 'large'" class="paperless-large-cards">
                            <div v-for="doc in documents" :key="doc.name"
                                 class="paperless-large-card"
                                 :class="{active: selected_doc && selected_doc.name === doc.name, selected: is_selected(doc.name)}"
                                 :data-type="get_file_type(doc)"
                                 @click="select_doc(doc)">
                                <input v-if="!provider" class="document-selector inline" type="checkbox" :checked="is_selected(doc.name)" @click.stop="toggle_selection(doc.name)" />
                                <div class="doc-icon-wrapper" :class="get_icon_class(doc)">
                                    <svg class="doc-type-icon" viewBox="0 0 32 38" aria-hidden="true" focusable="false">
                                        <path d="M5 1.5h15l7 7v28H5z M20 1.5v7h7" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" />
                                        <text x="16" y="26" text-anchor="middle">{{ get_icon(doc) }}</text>
                                    </svg>
                                </div>
                                <div class="large-card-body">
                                    <div class="doc-title">{{ doc.document_name }} <small class="text-muted" style="font-weight: normal;">({{ doc.name }})</small></div>
                                <component v-if="extensions.row_indicator" :is="extensions.row_indicator" :document="doc"></component>
                                    <div class="doc-meta-row">
                                        <span v-if="doc.document_code" class="doc-meta-item"><i class="fa fa-hashtag"></i> {{ doc.document_code }}</span>
                                        <span v-if="doc.ocr_status === 'Pending' || doc.ocr_status === 'Processing'" class="doc-meta-item" style="color: var(--pc-primary);"><i class="fa fa-spinner fa-spin"></i> ${__('OCR')}</span>
                                        <span class="doc-meta-item"><i class="fa fa-tag"></i> {{ doc.category }}</span>
                                        <span v-if="doc.party_type && doc.party_name" class="doc-meta-item"><i class="fa fa-handshake-o"></i> {{ doc.party_name }}</span>
                                        <span v-for="tag in doc.tags" class="keyword-badge tag-colored" :style="[get_tag_style(tag.color), {fontSize: '10px', padding: '1px 6px', margin: '0'}]">{{ tag.name }}</span>
                                    </div>
                                    <div class="search-excerpt" v-if="filters.search && doc.search_excerpt">
                                        <span v-if="doc.search_page">${__('Page')} {{ doc.search_page }}: </span><span v-html="highlight_text(doc.search_excerpt, doc.search_terms)"></span>
                                    </div>
                                </div>
                                <div class="large-card-actions">
                                    <button v-if="!provider" class="btn-action" @click.stop="open_doc(doc.name)" :title="'${__('Open in Frappe')}'"><i class="fa fa-external-link"></i></button>
                                    <a v-if="doc.original_file || doc.document_file" class="btn-action" :href="doc.original_download || doc.document_download || doc.original_file || doc.document_file" target="_blank" @click.stop :title="'${__('Download')}'"><i class="fa fa-download"></i></a>
                                </div>
                            </div>
                        </div>

                        <!-- List View -->
                        <div v-else class="paperless-list">
                            <table class="table table-hover">
                                <thead>
                                    <tr>
                                        <th v-if="!provider" style="width: 36px"><input type="checkbox" :checked="all_selected" @change="toggle_all" /></th>
                                        <th style="width: 44px"></th>
                                        <th>${__('Title')}</th>
                                        <th>${__('Category')}</th>
                                        <th>${__('Keywords')}</th>
                                        <th>${__('Date')}</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr v-for="doc in documents" :key="doc.name" 
                                        :class="{active: selected_doc && selected_doc.name === doc.name, selected: is_selected(doc.name)}"
                                        @click="select_doc(doc)">
                                        <td v-if="!provider"><input type="checkbox" :checked="is_selected(doc.name)" @click.stop="toggle_selection(doc.name)" /></td>
                                        <td><span class="list-icon" :class="get_icon_class(doc)"><svg class="doc-type-icon" viewBox="0 0 32 38" aria-hidden="true" focusable="false"><path d="M5 1.5h15l7 7v28H5z M20 1.5v7h7" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" /><text x="16" y="26" text-anchor="middle">{{ get_icon(doc) }}</text></svg></span></td>
                                        <td>
                                            <strong>{{ doc.document_name }} <small class="text-muted" style="font-weight: normal;">({{ doc.name }})</small></strong>
                                            <component v-if="extensions.row_indicator" :is="extensions.row_indicator" :document="doc"></component>
                                            <span v-if="doc.ocr_status === 'Pending' || doc.ocr_status === 'Processing'" style="display: block; font-size: 11px; margin-top: 2px; color: var(--pc-primary);"><i class="fa fa-spinner fa-spin"></i> ${__('Processing OCR')}</span>
                                            <span v-if="doc.document_code" class="text-muted" style="display: block; font-size: 11px; margin-top: 2px;"><i class="fa fa-hashtag"></i> {{ doc.document_code }}</span>
                                            <span v-if="doc.party_name" class="text-muted" style="display: block; font-size: 11px; margin-top: 2px;"><i class="fa fa-handshake-o"></i> {{ doc.party_name }}</span>
                                            <span v-if="filters.search && doc.search_excerpt" class="search-excerpt">
                                                <span v-if="doc.search_page">${__('Page')} {{ doc.search_page }}: </span><span v-html="highlight_text(doc.search_excerpt, doc.search_terms)"></span>
                                            </span>
                                        </td>
                                        <td>{{ doc.category }}</td>
                                        <td>
                                             <div style="display: flex; gap: 4px; flex-wrap: wrap;">
                                                 <span v-for="tag in doc.tags.slice(0, 3)" class="keyword-badge tag-colored" :style="[get_tag_style(tag.color), {fontSize: '10px', padding: '1px 6px', margin: '0', whiteSpace: 'nowrap'}]">{{ tag.name }}</span>
                                             </div>
                                         </td>
                                        <td style="white-space: nowrap">{{ format_date(doc.document_date) }}</td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>

                        <component v-if="extensions.list_footer" :is="extensions.list_footer"></component>
                        <!-- Pagination -->
                        <nav v-if="!extensions.list_footer && !loading && (current_page > 1 || has_more)" class="paperless-pagination" :aria-label="'${__('Document pages')}'">
                            <button class="btn btn-sm btn-default" :disabled="current_page <= 1" @click="go_to_page(current_page - 1)">
                                <i class="fa fa-chevron-left"></i> ${__('Previous')}
                            </button>
                            <span class="pagination-info" aria-live="polite">
                                ${__('Page')} {{ current_page }}<template v-if="total_pages > 1"> / {{ total_pages }}</template>
                            </span>
                            <button class="btn btn-sm btn-default" :disabled="!has_more" @click="go_to_page(current_page + 1)">
                                ${__('Next')} <i class="fa fa-chevron-right"></i>
                            </button>
                        </nav>
                    </div>

                    <!-- Right: Sidebar -->
                    <div class="paperless-sidebar" v-if="selected_doc" :class="sidebar_class" :key="selected_doc.name">
                        <div class="sidebar-header">
                            <h4>{{ selected_doc.document_name }}</h4>
                            <button class="btn-close-sidebar" @click="select_doc(null)"><i class="fa fa-times"></i></button>
                        </div>
                        <div class="sidebar-body">
                            <component v-if="extensions.sidebar_top" :is="extensions.sidebar_top"></component>
                            <!-- Preview (inline) -->
                            <div class="doc-preview" v-if="selected_doc.document_file && !is_fullscreen">
                                <button class="fullscreen-btn" @click="is_fullscreen = true" :title="'${__('Toggle Full Screen')}'">
                                    <i class="fa fa-expand"></i>
                                </button>

                                <!-- PDF Viewer -->
                                <div v-if="is_pdf(selected_doc.document_file)" style="width: 100%; height: 100%; display: flex; flex-direction: column; min-width: 0;">
                                    <div v-if="is_office(selected_doc.original_file)" class="alert alert-warning" style="flex-shrink: 0;">
                                        <i class="fa fa-info-circle"></i> 
                                        <strong>${__('Preview Notice:')}</strong> ${__('You are viewing an automatically generated preview from an Office file. Some complex formatting or design elements might not display with 100% accuracy. For perfect fidelity, we recommend downloading the original file from the sidebar.')}
                                    </div>
                                    <loan-pdf-viewer
                                        :key="selected_doc.document_download || selected_doc.document_file"
                                        :src="selected_doc.document_download || selected_doc.document_file"
                                        :query="filters.search"
                                        :terms="selected_doc.search_terms || []"
                                        :page="selected_doc.search_page || 1"
                                        :max-scale="1.75">
                                    </loan-pdf-viewer>
                                </div>
                                <img v-else-if="is_img(selected_doc.document_file)" :src="selected_doc.document_download || selected_doc.document_file" />
                                
                                <!-- Txt/Md Viewer -->
                                <div v-else-if="is_txt(selected_doc.document_file) || is_md(selected_doc.document_file, selected_doc)" class="text-preview-container">
                                    <div v-if="loading_text_preview" class="text-preview-loading">
                                        <i class="fa fa-spinner fa-spin"></i> ${__('Loading text...')}
                                    </div>
                                    <div v-else-if="is_md(selected_doc.document_file, selected_doc)" class="markdown-preview-body" @click="handle_markdown_click" v-html="render_markdown(text_preview_content)"></div>
                                    <pre v-else class="text-preview-pre">{{ text_preview_content }}</pre>
                                </div>

                                <!-- Office Formats (No PDF generated yet) -->
                                <div v-else-if="is_office(selected_doc.document_file)" class="office-placeholder">
                                    <div class="placeholder-icon"><i class="fa fa-file-text-o"></i></div>
                                    <h5>${__('Office Preview')}</h5>
                                    <p>${__('The PDF file for secure preview has not been generated yet or is processing.')}</p>
                                    <p style="margin-top: 6px;"><strong>${__('Please wait a few seconds and refresh the page, or download the original file using the sidebar button.')}</strong></p>
                                </div>
                                <div v-else class="office-placeholder">
                                    <div class="placeholder-icon"><i class="fa fa-file-o"></i></div>
                                    <h5>${__('Preview not available')}</h5>
                                    <p>${__('This file type cannot be previewed. Please download it to view.')}</p>
                                </div>
                            </div>
                            <!-- Placeholder when fullscreen is active -->
                            <div v-else-if="selected_doc.document_file && is_fullscreen" class="doc-preview" style="cursor: pointer;" @click="is_fullscreen = false">
                                <div class="office-placeholder">
                                    <div class="placeholder-icon"><i class="fa fa-expand"></i></div>
                                    <h5>${__('Fullscreen mode active')}</h5>
                                    <p>${__('Click here to exit fullscreen.')}</p>
                                </div>
                            </div>
                            <div v-else class="office-placeholder" style="min-height: 200px;">
                                <div class="placeholder-icon"><i class="fa fa-ban"></i></div>
                                <h5>${__('No file attached')}</h5>
                                <p>${__('This document does not have any file attached yet.')}</p>
                            </div>

                            <!-- Fullscreen Teleport (renders on body to escape sidebar transform) -->
                            <teleport to="body">
                                <div v-if="is_fullscreen && selected_doc && selected_doc.document_file" class="pc-fullscreen-overlay" @click.self="is_fullscreen = false">
                                    <button class="pc-fullscreen-close" @click="is_fullscreen = false" :title="'${__('Close Fullscreen')}'">
                                        <i class="fa fa-times"></i>
                                    </button>
                                    <div class="pc-fullscreen-content">
                                        <div v-if="is_pdf(selected_doc.document_file)" class="pc-fullscreen-pdf-wrapper">
                                            <div v-if="is_office(selected_doc.original_file)" class="alert alert-warning" style="margin-bottom: 12px; flex-shrink: 0; width: 100%;">
                                                <i class="fa fa-info-circle"></i> 
                                                <strong>${__('Preview Notice:')}</strong> ${__('You are viewing an automatically generated preview from an Office file. Some complex formatting or design elements might not display with 100% accuracy. For perfect fidelity, we recommend downloading the original file from the sidebar.')}
                                            </div>
                                            <loan-pdf-viewer
                                                :key="selected_doc.document_download || selected_doc.document_file"
                                                :src="selected_doc.document_download || selected_doc.document_file"
                                                :query="filters.search"
                                                :terms="selected_doc.search_terms || []"
                                                :page="selected_doc.search_page || 1"
                                                :max-scale="3">
                                            </loan-pdf-viewer>
                                        </div>
                                        <img v-else-if="is_img(selected_doc.document_file)" :src="selected_doc.document_download || selected_doc.document_file" />
                                        
                                        <!-- Txt/Md Viewer (Fullscreen) -->
                                        <div v-else-if="is_txt(selected_doc.document_file) || is_md(selected_doc.document_file, selected_doc)" class="text-preview-container fullscreen">
                                            <div v-if="loading_text_preview" class="text-preview-loading">
                                                <i class="fa fa-spinner fa-spin"></i> ${__('Loading text...')}
                                            </div>
                                            <div v-else-if="is_md(selected_doc.document_file, selected_doc)" class="markdown-preview-body" @click="handle_markdown_click" v-html="render_markdown(text_preview_content)"></div>
                                            <pre v-else class="text-preview-pre">{{ text_preview_content }}</pre>
                                        </div>

                                        <div v-else-if="is_office(selected_doc.document_file)" class="office-placeholder">
                                            <div class="placeholder-icon"><i class="fa fa-file-text-o"></i></div>
                                            <h5>${__('Office Preview')}</h5>
                                            <p>${__('The PDF file for secure preview has not been generated yet or is processing.')}</p>
                                            <p style="margin-top: 6px;"><strong>${__('Please wait a few seconds and refresh the page, or download the original file using the sidebar button.')}</strong></p>
                                        </div>
                                        <div v-else class="office-placeholder">
                                            <div class="placeholder-icon"><i class="fa fa-file-o"></i></div>
                                            <h5>${__('Preview not available')}</h5>
                                            <p>${__('This file type cannot be previewed. Please download it to view.')}</p>
                                        </div>
                                    </div>
                                </div>
                            </teleport>

                            <!-- Metadata -->
                            <div class="doc-metadata">
                                <div class="meta-item">
                                    <div class="meta-icon"><i class="fa fa-id-card-o"></i></div>
                                    <div class="meta-content">
                                        <div class="meta-label">${__('Document #')}</div>
                                        <div class="meta-value" style="font-family: monospace;">{{ selected_doc.name }}</div>
                                    </div>
                                </div>
                                <div class="meta-item" v-if="selected_doc.document_code">
                                    <div class="meta-icon"><i class="fa fa-hashtag"></i></div>
                                    <div class="meta-content">
                                        <div class="meta-label">${__('Code')}</div>
                                        <div class="meta-value">{{ selected_doc.document_code }}</div>
                                    </div>
                                </div>
                                <div class="meta-item">
                                    <div class="meta-icon"><i class="fa fa-tag"></i></div>
                                    <div class="meta-content">
                                        <div class="meta-label">${__('Category')}</div>
                                        <div class="meta-value">{{ selected_doc.category }}</div>
                                    </div>
                                </div>
                                <component v-if="extensions.sidebar_metadata" :is="extensions.sidebar_metadata"></component>
                                <div class="meta-item" v-if="selected_doc.folder">
                                    <div class="meta-icon"><i class="fa fa-folder"></i></div>
                                    <div class="meta-content">
                                        <div class="meta-label">${__('Folder')}</div>
                                        <div class="meta-value">{{ selected_doc.folder }}</div>
                                    </div>
                                </div>
                                <div class="meta-item" v-if="department_available && selected_doc.department">
                                    <div class="meta-icon"><i class="fa fa-building-o"></i></div>
                                    <div class="meta-content">
                                        <div class="meta-label">${__('Department')}</div>
                                        <div class="meta-value">{{ selected_doc.department }}</div>
                                    </div>
                                </div>
                                <div class="meta-item">
                                    <div class="meta-icon"><i class="fa fa-circle"></i></div>
                                    <div class="meta-content">
                                        <div class="meta-label">${__('Status')}</div>
                                        <div class="meta-value"><span class="status-pill" :class="status_class(selected_doc.status)">{{ selected_doc.status }}</span></div>
                                    </div>
                                </div>
                                <div class="meta-item">
                                    <div class="meta-icon"><i class="fa fa-code-fork"></i></div>
                                    <div class="meta-content">
                                        <div class="meta-label">${__('Version')}</div>
                                        <div class="meta-value">{{ selected_doc.version || '-' }}</div>
                                    </div>
                                </div>
                                <div class="meta-item" v-if="selected_doc.party_type && selected_doc.party_name">
                                    <div class="meta-icon"><i class="fa fa-handshake-o"></i></div>
                                    <div class="meta-content">
                                        <div class="meta-label">${__('Correspondent')}</div>
                                        <div class="meta-value">
                                            <a href="#" @click.prevent="open_party(selected_doc.party_type, selected_doc.party_name)">
                                                {{ __(selected_doc.party_type) }}: {{ selected_doc.party_name }}
                                            </a>
                                        </div>
                                    </div>
                                </div>
                                <div class="meta-item" v-if="selected_doc.expiry_date">
                                    <div class="meta-icon"><i class="fa fa-calendar-times-o"></i></div>
                                    <div class="meta-content">
                                        <div class="meta-label">${__('Expiry Date')}</div>
                                        <div class="meta-value">{{ format_date(selected_doc.expiry_date) }}</div>
                                    </div>
                                </div>
                                <div class="meta-item" v-if="selected_doc.review_date">
                                    <div class="meta-icon"><i class="fa fa-calendar-check-o"></i></div>
                                    <div class="meta-content">
                                        <div class="meta-label">${__('Review Date')}</div>
                                        <div class="meta-value">{{ format_date(selected_doc.review_date) }}</div>
                                    </div>
                                </div>
                                <div class="meta-item" v-if="selected_doc.tags && selected_doc.tags.length > 0">
                                    <div class="meta-icon"><i class="fa fa-tags"></i></div>
                                    <div class="meta-content">
                                        <div class="meta-label">${__('Tags')}</div>
                                        <div class="meta-value" style="margin-top: 4px; display: flex; flex-wrap: wrap; gap: 4px;">
                                            <span v-for="tag in selected_doc.tags" class="keyword-badge tag-colored" :style="get_tag_style(tag.color)">{{ tag.name }}</span>
                                        </div>
                                    </div>
                                </div>
                            </div>
                            <component v-if="extensions.sidebar_bottom" :is="extensions.sidebar_bottom"></component>
                            <div class="doc-versions-section" v-if="!provider && selected_versions.length">
                                <div class="meta-label" style="margin: 12px 0 6px; font-weight: 600;">${__('Version History')} ({{ selected_versions.length }})</div>
                                <div class="version-history-list">
                                    <div v-for="ver in selected_versions" :key="ver.name" class="version-history-item" style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--pc-border-light);border-radius:6px;margin-bottom:6px;font-size:12px;">
                                        <div style="min-width:0;">
                                            <strong>v{{ ver.version_number }}</strong>
                                            <span class="text-muted" v-if="ver.release_date"> · {{ format_date(ver.release_date) }}</span>
                                            <div class="text-muted" v-if="ver.change_log" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{{ ver.change_log }}</div>
                                        </div>
                                        <div style="display:flex;gap:4px;flex-shrink:0;">
                                            <button v-if="ver.preview_url || ver.download_url" class="btn btn-xs btn-default" @click="preview_version(ver)" :title="'${__('Preview version')}'">
                                                <i class="fa fa-eye"></i>
                                            </button>
                                            <a v-if="ver.download_url" :href="ver.download_url" target="_blank" class="btn btn-xs btn-default" :title="'${__('Download')}'">
                                                <i class="fa fa-download"></i>
                                            </a>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                        <div class="sidebar-footer">
                            <component v-if="extensions.sidebar_actions" :is="extensions.sidebar_actions"></component>
                            <template v-if="!provider">
                            <button v-if="!trash_mode" class="btn btn-default" @click="quick_edit_doc(selected_doc)">
                                <i class="fa fa-pencil"></i> ${__('Quick Edit')}
                            </button>
                            <button v-if="!trash_mode" class="btn btn-default" @click="upload_new_version(selected_doc)">
                                <i class="fa fa-upload"></i> ${__('Upload New Version')}
                            </button>
                            <button v-if="!trash_mode" class="btn btn-default" @click="view_ocr_text(selected_doc)">
                                <i class="fa fa-align-left"></i> ${__('View OCR Text')}
                            </button>
                            <button v-if="!trash_mode" class="btn btn-copy-link" @click="copy_link(selected_doc)">
                                <i class="fa fa-clipboard"></i> ${__('Copy Link')}
                            </button>
                            <button v-if="!trash_mode" class="btn btn-default" @click="manage_share_links(selected_doc)">
                                <i class="fa fa-share-alt"></i> ${__('External Share')}
                            </button>
                            <button v-if="!trash_mode" class="btn btn-default" @click="reprocess_ocr(selected_doc.name)" :disabled="['Pending', 'Processing'].includes(selected_doc.ocr_status)">
                                <i class="fa fa-file-text-o"></i> ${__('Reprocess OCR')}
                            </button>
                            <button v-if="!trash_mode && is_office(selected_doc.original_file) && !is_pdf(selected_doc.document_file)" class="btn btn-generate" @click="force_pdf(selected_doc.name)">
                                <i class="fa fa-refresh"></i> ${__('Generate PDF (LibreOffice)')}
                            </button>
                            <button v-if="!trash_mode" class="btn btn-open" @click="open_doc(selected_doc.name)">
                                <i class="fa fa-external-link"></i> ${__('Open in Frappe')}
                            </button>
                            <button v-if="!trash_mode" class="btn btn-danger" @click="trash_one(selected_doc.name)">
                                <i class="fa fa-trash"></i> ${__('Move to trash')}
                            </button>
                            <button v-if="trash_mode" class="btn btn-primary" @click="restore_one(selected_doc.name)">
                                <i class="fa fa-undo"></i> ${__('Restore')}
                            </button>
                            <button v-if="trash_mode && can_purge" class="btn btn-danger" @click="purge_one(selected_doc.name)">
                                <i class="fa fa-times"></i> ${__('Delete permanently')}
                            </button>
                            <a v-if="selected_doc.original_file || selected_doc.document_file" :href="selected_doc.original_download || selected_doc.original_file || selected_doc.document_download || selected_doc.document_file" target="_blank" class="btn btn-download">
                                <i class="fa fa-download"></i> ${__('Download Original File')}
                            </a>
                            </template>
                        </div>
                    </div>
                </div>

                <!-- Upload Modal -->
                <teleport to="body">
                    <div v-if="show_upload_modal" class="pc-modal-overlay" @click.self="close_upload_modal">
                        <div class="pc-modal">
                            <div class="pc-modal-header">
                                <h4>${__('Upload Document')}</h4>
                                <button class="pc-modal-close" @click="close_upload_modal"><i class="fa fa-times"></i></button>
                            </div>
                            <div class="pc-modal-body">
                                <div class="pc-drop-zone"
                                     @dragover.prevent="dragover = true"
                                     @dragleave.prevent="dragover = false"
                                     @drop.prevent="handle_drop"
                                     @click="$refs.fileInput.click()"
                                     :class="{ dragover: dragover }">
                                    <div class="pc-drop-icon"><i class="fa fa-cloud-upload"></i></div>
                                    <p class="pc-drop-text">${__('Drag & Drop your file here or click to browse')}</p>
                                    <input type="file" ref="fileInput" multiple style="display: none" @change="handle_file_select">
                                </div>
                                <div v-if="upload_form.files.length" style="margin-top: 10px; max-height: 120px; overflow-y: auto;">
                                    <div v-for="(f, idx) in upload_form.files" :key="f.name + idx" class="pc-file-preview" style="margin-top: 6px;">
                                        <i class="fa fa-file-text-o"></i>
                                        <div class="pc-file-name">{{ f.name }}</div>
                                        <button class="pc-file-remove" @click.prevent="remove_file(idx)"><i class="fa fa-times"></i></button>
                                    </div>
                                </div>

                                <div class="pc-form-group" v-if="upload_form.files.length <= 1">
                                    <label>${__('Document Code (Optional)')}</label>
                                    <input type="text" class="pc-form-control" v-model="upload_form.document_code" placeholder="${__('Leave blank to auto-generate')}">
                                </div>
                                <div class="pc-form-group" v-if="upload_form.files.length <= 1">
                                    <label>${__('Title *')}</label>
                                    <input type="text" class="pc-form-control" v-model="upload_form.title" placeholder="${__('Document Title')}" required>
                                </div>
                                <div class="pc-form-group">
                                    <label>${__('Category')} <span style="font-weight:normal;color:#888;">${__('(Optional)')}</span></label>
                                    <select class="pc-form-control" v-model="upload_form.category">
                                        <option value="">${__('No Category')}</option>
                                        <option v-for="cat in categories" :value="cat.name">{{ cat.name }}</option>
                                    </select>
                                </div>
                                <div class="pc-form-group">
                                    <label>${__('Status')}</label>
                                    <select class="pc-form-control" v-model="upload_form.status">
                                        <option value="Draft">${__('Draft')}</option>
                                        <option value="Published">${__('Published')}</option>
                                        <option value="Obsolete">${__('Obsolete')}</option>
                                    </select>
                                </div>
                                <div class="pc-form-group">
                                    <label>${__('Folder')} <span style="font-weight:normal;color:#888;">${__('(Optional)')}</span></label>
                                    <select v-model="upload_form.folder" class="pc-form-control">
                                        <option value="">${__('Home/Attachments (Default)')}</option>
                                        <option v-for="fld in folders" :value="fld.folder_name">{{ fld.file_name }}</option>
                                    </select>
                                </div>
                                <div class="pc-form-group" v-if="tags.length">
                                    <label>${__('Tags')} <span style="font-weight:normal;color:#888;">${__('(Optional)')}</span></label>
                                    <div style="display:flex;flex-wrap:wrap;gap:6px;max-height:90px;overflow-y:auto;">
                                        <label v-for="tag in tags" :key="tag.name" style="display:inline-flex;align-items:center;gap:4px;font-weight:normal;margin:0;cursor:pointer;">
                                            <input type="checkbox" :value="tag.name" v-model="upload_form.tags" />
                                            <span class="keyword-badge tag-colored" :style="[get_tag_style(tag.color), {fontSize:'11px',padding:'1px 6px',margin:0}]">{{ tag.name }}</span>
                                        </label>
                                    </div>
                                </div>
                                <div class="pc-form-group">
                                    <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;">
                                        <input type="checkbox" v-model="upload_form.only_me" />
                                        <span>${__('Only Me')}</span>
                                    </label>
                                </div>
                            </div>
                            <div class="pc-modal-footer">
                                <button class="btn btn-default" @click="close_upload_modal">${__('Cancel')}</button>
                                <button class="btn-upload" @click="submit_upload" :disabled="!upload_form.files.length || (upload_form.files.length === 1 && !upload_form.title) || is_uploading">
                                    <i class="fa fa-spinner fa-spin" v-if="is_uploading"></i>
                                    <i class="fa fa-upload" v-else></i>
                                    {{ is_uploading ? '${__('Uploading...')}' : '${__('Upload & Save')}' }}
                                </button>
                            </div>
                        </div>
                    </div>
                </teleport>

            </div>
        `;

        this.wrapper.html(`<div id="document-management-vue-app">${template}</div>`);

        const { createApp, ref, reactive, computed, onMounted, onBeforeUnmount } = Vue;

        const console_controller = this;
        const app = createApp({
            setup() {
                const route_params = new URLSearchParams(window.location.search);
                let requested_document = route_params.get('document') || route_params.get('Doc') || route_params.get('doc');
                const requested_query = (route_params.get('query') || '').trim();
                const requested_page = Number(route_params.get('page')) || null;
                const documents = ref([]);
                const current_page = ref(1);
                const page_size = provider?.page_size || 50;
                const total_documents = ref(0);
                const has_more = ref(false);
                const sort_by = ref('modified_desc');
                const total_pages = computed(() => Math.max(1, Math.ceil((total_documents.value || 0) / page_size)));
                const categories = ref([]);
                const tags = ref([]);
                const selected_doc = ref(null);
                const selected_versions = ref([]);
                const department_available = ref(true);
                const view_mode = ref('grid');
                const loading = ref(false);
                const trash_mode = ref(false);
                const saved_views = ref([]);
                const selected_saved_view = ref('');
                const selected_names = ref(new Set());
                const can_purge = (frappe.user_roles || []).includes('System Manager');
                const selected_count = computed(() => selected_names.value.size);
                const all_selected = computed(() =>
                    documents.value.length > 0 &&
                    documents.value.every((doc) => selected_names.value.has(doc.name))
                );
                const filters = reactive({
                    search: requested_query,
                    categories: [],
                    statuses: [],
                    tags: []
                });

                const show_category_dropdown = ref(false);
                const show_status_dropdown = ref(false);
                const show_tag_dropdown = ref(false);

                const toggle_category_dropdown = () => {
                    show_category_dropdown.value = !show_category_dropdown.value;
                    show_status_dropdown.value = false;
                    show_tag_dropdown.value = false;
                };
                const toggle_status_dropdown = () => {
                    show_status_dropdown.value = !show_status_dropdown.value;
                    show_category_dropdown.value = false;
                    show_tag_dropdown.value = false;
                };
                const toggle_tag_dropdown = () => {
                    show_tag_dropdown.value = !show_tag_dropdown.value;
                    show_category_dropdown.value = false;
                    show_status_dropdown.value = false;
                };

                const clear_categories = () => {
                    filters.categories = [];
                    fetch_documents(true);
                };
                const clear_statuses = () => {
                    filters.statuses = [];
                    fetch_documents(true);
                };
                const clear_tags = () => {
                    filters.tags = [];
                    fetch_documents(true);
                };

                const handle_window_click = (e) => {
                    if (show_tag_dropdown.value && !e.target.closest('.tags-dropdown-container')) {
                        show_tag_dropdown.value = false;
                    }
                    if (show_category_dropdown.value && !e.target.closest('.categories-dropdown-container')) {
                        show_category_dropdown.value = false;
                    }
                    if (show_status_dropdown.value && !e.target.closest('.statuses-dropdown-container')) {
                        show_status_dropdown.value = false;
                    }
                };

                const show_upload_modal = ref(false);
                const is_uploading = ref(false);
                const dragover = ref(false);
                const upload_form = reactive({
                    files: [],
                    title: '',
                    category: '',
                    status: 'Draft',
                    folder: '',
                    document_code: '',
                    tags: [],
                    only_me: false
                });

                const folders = ref([]);

                const close_upload_modal = () => {
                    show_upload_modal.value = false;
                    upload_form.files = [];
                    upload_form.title = '';
                    upload_form.category = '';
                    upload_form.status = 'Draft';
                    upload_form.folder = '';
                    upload_form.document_code = '';
                    upload_form.tags = [];
                    upload_form.only_me = false;
                };

                const add_files = (file_list) => {
                    const incoming = Array.from(file_list || []);
                    if (!incoming.length) return;
                    upload_form.files = [...upload_form.files, ...incoming];
                    if (upload_form.files.length === 1 && !upload_form.title) {
                        upload_form.title = upload_form.files[0].name.replace(/\.[^/.]+$/, "");
                    }
                };

                const handle_drop = (e) => {
                    dragover.value = false;
                    add_files(e.dataTransfer.files);
                };

                const handle_file_select = (e) => {
                    add_files(e.target.files);
                    if (e.target) e.target.value = '';
                };

                const remove_file = (index) => {
                    upload_form.files.splice(index, 1);
                };

                const submit_upload = async () => {
                    if (!upload_form.files.length) return;
                    if (upload_form.files.length === 1 && !upload_form.title) return;
                    is_uploading.value = true;

                    let uploaded = 0;
                    const errors = [];
                    try {
                        for (const file of upload_form.files) {
                            const doc_title = upload_form.files.length === 1
                                ? upload_form.title
                                : file.name.replace(/\.[^/.]+$/, "");
                            try {
                                await upload_document_file(
                                    file,
                                    'document_management.frappe_document_management.page.document_management_console.document_management_console.quick_upload',
                                    {
                                        title: doc_title,
                                        category: upload_form.category,
                                        status: upload_form.status || 'Draft',
                                        folder: upload_form.folder || '',
                                        document_code: upload_form.files.length === 1 ? (upload_form.document_code || '') : '',
                                        tags: JSON.stringify(upload_form.tags || []),
                                        only_me: upload_form.only_me ? '1' : '0'
                                    }
                                );
                                uploaded += 1;
                            } catch (err) {
                                errors.push(`${file.name}: ${err?.message || __('Upload failed')}`);
                            }
                        }
                        if (uploaded > 0) {
                            frappe.show_alert({
                                message: uploaded === 1
                                    ? __('Document uploaded successfully')
                                    : __('Uploaded {0} documents', [uploaded]),
                                indicator: 'green'
                            });
                            fetch_documents(true);
                        }
                        if (errors.length) {
                            frappe.msgprint({
                                title: __('Some uploads failed'),
                                indicator: 'red',
                                message: errors.map((e) => `<div>${escape_html(e)}</div>`).join('')
                            });
                        } else {
                            close_upload_modal();
                        }
                    } finally {
                        is_uploading.value = false;
                    }
                };

                let debounceTimer;
                let realtimeRefreshTimer;
                const realtime_event = 'document_management_document_changed';

                const fetch_documents = (reset_page = false) => {
                    if (provider) return adapter.load_documents(reset_page === true);
                    if (reset_page === true) {
                        current_page.value = 1;
                    }
                    loading.value = true;
                    frappe.call({
                        method: 'document_management.frappe_document_management.page.document_management_console.document_management_console.get_documents',
                        args: {
                            search_text: filters.search,
                            categories: filters.categories.length ? JSON.stringify(filters.categories) : null,
                            statuses: filters.statuses.length ? JSON.stringify(filters.statuses) : null,
                            tags: filters.tags.length ? JSON.stringify(filters.tags) : null,
                            trash: trash_mode.value ? 1 : 0,
                            limit: page_size,
                            start: (current_page.value - 1) * page_size,
                            include_meta: 1,
                            sort_by: sort_by.value
                        },
                        callback: (r) => {
                            const payload = r.message;
                            if (Array.isArray(payload)) {
                                documents.value = payload;
                                total_documents.value = payload.length;
                                has_more.value = false;
                            } else {
                                documents.value = (payload && payload.documents) || [];
                                total_documents.value = Number((payload && payload.total) ?? documents.value.length);
                                has_more.value = Boolean(payload && payload.has_more);
                            }
                            selected_names.value = new Set();
                            if (requested_document) {
                                const requested = documents.value.find(
                                    (doc) => doc.name === requested_document
                                );
                                if (requested) {
                                    if (requested_page && !requested.search_page) {
                                        requested.search_page = requested_page;
                                    }
                                    select_doc(requested);
                                    is_fullscreen.value = true;
                                    requested_document = null;
                                } else {
                                    frappe.call({
                                        method: 'document_management.frappe_document_management.page.document_management_console.document_management_console.get_documents',
                                        args: { search_text: requested_document },
                                        callback: (res) => {
                                            const req_doc = res.message && res.message[0];
                                            if (req_doc && req_doc.name === requested_document) {
                                                if (requested_page) {
                                                    req_doc.search_page = requested_page;
                                                }
                                                documents.value.unshift(req_doc);
                                                select_doc(req_doc);
                                                is_fullscreen.value = true;
                                            }
                                            requested_document = null;
                                            loading.value = false;
                                        },
                                        error: () => {
                                            requested_document = null;
                                            loading.value = false;
                                        }
                                    });
                                    return;
                                }
                            }
                            if (selected_doc.value &&
                                documents.value.some((doc) => doc.name === selected_doc.value.name)) {
                                sync_selected_doc();
                            } else if (selected_doc.value) {
                                select_doc(null);
                            }
                            loading.value = false;
                        },
                        error: () => {
                            loading.value = false;
                            frappe.msgprint(__('Failed to load documents. Please try again.'));
                        }
                    });
                };

                const fetch_categories = () => {
                    frappe.call({
                        method: 'document_management.frappe_document_management.page.document_management_console.document_management_console.get_categories',
                        callback: (r) => {
                            categories.value = r.message || [];
                        }
                    });
                };

                const fetch_saved_views = () => {
                    frappe.call({
                        method: 'document_management.frappe_document_management.page.document_management_console.document_management_console.list_saved_views',
                        callback: (r) => {
                            saved_views.value = r.message || [];
                        }
                    });
                };

                const apply_saved_view = () => {
                    const view = saved_views.value.find(
                        (row) => row.name === selected_saved_view.value
                    );
                    if (!view) return;
                    let values = {};
                    try {
                        values = JSON.parse(view.filters_json || '{}');
                    } catch (e) {
                        frappe.msgprint(__('Saved view is invalid.'));
                        return;
                    }
                    filters.search = values.search || '';
                    if (values.categories) {
                        filters.categories = values.categories;
                    } else if (values.category) {
                        filters.categories = [values.category];
                    } else {
                        filters.categories = [];
                    }

                    if (values.statuses) {
                        filters.statuses = values.statuses;
                    } else if (values.status) {
                        filters.statuses = [values.status];
                    } else {
                        filters.statuses = [];
                    }
                    filters.tags = values.tags || [];
                    trash_mode.value = Boolean(values.trash);
                    if (values.view_mode) {
                        view_mode.value = values.view_mode;
                    }
                    selected_doc.value = null;
                    fetch_documents(true);
                };

                const save_view = () => {
                    frappe.prompt(
                        [
                            {
                                fieldname: 'view_name',
                                fieldtype: 'Data',
                                label: __('View name'),
                                reqd: 1
                            },
                            {
                                fieldname: 'view_mode',
                                fieldtype: 'Select',
                                label: __('View Type'),
                                options: [
                                    { value: 'grid', label: __('Grid') },
                                    { value: 'large', label: __('List Card') },
                                    { value: 'list', label: __('Table') }
                                ],
                                default: view_mode.value,
                                reqd: 1
                            }
                        ],
                        (values) => {
                            frappe.call({
                                method: 'document_management.frappe_document_management.page.document_management_console.document_management_console.save_current_view',
                                args: {
                                    view_name: values.view_name,
                                    filters: JSON.stringify({
                                        search: filters.search,
                                        categories: filters.categories,
                                        statuses: filters.statuses,
                                        tags: filters.tags,
                                        view_mode: values.view_mode,
                                        trash: trash_mode.value ? 1 : 0
                                    })
                                },
                                callback: (r) => {
                                    if (!r.exc) {
                                        selected_saved_view.value = r.message.name;
                                        fetch_saved_views();
                                        frappe.show_alert({
                                            message: __('View saved'),
                                            indicator: 'green'
                                        });
                                    }
                                }
                            });
                        },
                        __('Save current view')
                    );
                };

                const delete_view = () => {
                    const name = selected_saved_view.value;
                    if (!name) return;
                    frappe.confirm(__('Delete this saved view?'), () => {
                        frappe.call({
                            method: 'document_management.frappe_document_management.page.document_management_console.document_management_console.delete_saved_view',
                            args: {view: name},
                            callback: (r) => {
                                if (!r.exc) {
                                    selected_saved_view.value = '';
                                    fetch_saved_views();
                                }
                            }
                        });
                    });
                };

                const debounce_fetch = () => {
                    clearTimeout(debounceTimer);
                    debounceTimer = setTimeout(() => fetch_documents(true), 300);
                };

                const go_to_page = (target_page) => {
                    const next_page = Math.max(1, Number(target_page) || 1);
                    if (next_page === current_page.value) return;
                    current_page.value = next_page;
                    fetch_documents(false);
                };

                const show_search_help = () => {
                    if (adapter?.show_search_help) return adapter.show_search_help();
                    frappe.msgprint({
                        title: __('Advanced Search Syntax'),
                        message: `
                            <p>${__('Combine free text with field filters. Prefix any filter with <code>-</code> to exclude matches.')}</p>
                            <table class="table table-bordered table-condensed" style="font-size: 12px;">
                                <tbody>
                                    <tr><td><code>status:Published</code></td><td>${__('Filter by status (Draft, Published, Obsolete)')}</td></tr>
                                    <tr><td><code>category:Legal</code></td><td>${__('Filter by category')}</td></tr>
                                    <tr><td><code>tag:Urgent -tag:Archived</code></td><td>${__('Include or exclude tags')}</td></tr>
                                    <tr><td><code>title:"loan agreement"</code></td><td>${__('Search within title')}</td></tr>
                                    <tr><td><code>code:LN-2026</code></td><td>${__('Search by document code')}</td></tr>
                                    <tr><td><code>ocr:"signed by"</code></td><td>${__('Search within OCR content')}</td></tr>
                                    <tr><td><code>owner:user@example.com</code></td><td>${__('Filter by document owner')}</td></tr>
                                    <tr><td><code>created:2026-01-01..2026-01-31</code></td><td>${__('Filter by creation or modified date range')}</td></tr>
                                </tbody>
                            </table>
                        `
                    });
                };

                const text_preview_content = ref('');
                const loading_text_preview = ref(false);

                async function load_text_preview(url) {
                    if (!url) {
                        text_preview_content.value = '';
                        return;
                    }
                    loading_text_preview.value = true;
                    text_preview_content.value = '';
                    try {
                        const response = await fetch(url);
                        if (response.ok) {
                            text_preview_content.value = await response.text();
                        } else {
                            text_preview_content.value = `Failed to load preview: ${response.statusText}`;
                        }
                    } catch (err) {
                        text_preview_content.value = `Failed to load preview: ${err.message}`;
                    } finally {
                        loading_text_preview.value = false;
                    }
                }

                function fetch_versions(doc_name) {
                    if (!doc_name) {
                        selected_versions.value = [];
                        return;
                    }
                    frappe.call({
                        method: 'document_management.frappe_document_management.page.document_management_console.document_management_console.get_document_versions',
                        args: {doc_name},
                        callback: (r) => {
                            if (selected_doc.value && selected_doc.value.name === doc_name) {
                                selected_versions.value = r.message || [];
                            }
                        }
                    });
                }

                function select_doc(doc) {
                    if (provider) return adapter.select_document(doc);
                    selected_doc.value = doc;
                    selected_versions.value = [];
                    const file_path = doc ? (doc.original_file || doc.document_file) : null;
                    const preview_url = doc ? (doc.original_download || doc.document_download || file_path) : null;
                    if (doc && doc.name) {
                        fetch_versions(doc.name);
                    }
                    if (file_path && (is_txt(file_path) || is_md(file_path, doc))) {
                        load_text_preview(preview_url);
                    } else {
                        text_preview_content.value = '';
                    }
                }

                const preview_version = (ver) => {
                    if (!ver || !selected_doc.value) return;
                    const doc = selected_doc.value;
                    const url = ver.preview_url || (ver.download_url ? ver.download_url.replace(/([?&])attachment=1(&?)/, '$1attachment=0$2') : '');
                    if (!url) return;
                    const isPdfPreview = Boolean(ver.has_preview || (doc.original_file && is_pdf(doc.original_file)));
                    let mountedApp = null;
                    const dialog = new frappe.ui.Dialog({
                        title: `${escape_html(doc.document_name || doc.name)} · v${escape_html(ver.version_number)}`,
                        size: 'extra-large',
                        fields: [{fieldname: 'viewer_html', fieldtype: 'HTML'}],
                        on_hide: () => {
                            if (mountedApp) {
                                try { mountedApp.unmount(); } catch (e) {}
                                mountedApp = null;
                            }
                        }
                    });
                    dialog.show();
                    const container = dialog.fields_dict.viewer_html.$wrapper[0];
                    if (isPdfPreview && window.LoanManagerPdfSearchViewer) {
                        container.innerHTML = '<div style="height: 72vh; position: relative;"></div>';
                        const mountTarget = container.firstElementChild;
                        mountedApp = createApp({
                            template: '<loan-pdf-viewer :src="src" :max-scale="3"></loan-pdf-viewer>',
                            setup() { return {src: ref(url)}; }
                        });
                        mountedApp.component('loan-pdf-viewer', window.LoanManagerPdfSearchViewer.createVueComponent(Vue));
                        mountedApp.mount(mountTarget);
                    } else {
                        container.innerHTML = `<iframe src="${escape_html(url)}" style="width:100%;height:72vh;border:0;border-radius:6px;"></iframe>`;
                    }
                };

                const quick_edit_doc = (doc) => {
                    if (!doc) return;
                    const currentTagSet = new Set((doc.tags || []).map((t) => t.name));
                    const fields = [
                        {fieldname: 'title', fieldtype: 'Data', label: __('Title'), default: doc.document_name, reqd: 1},
                        {fieldname: 'document_code', fieldtype: 'Data', label: __('Document Code'), default: doc.document_code || ''},
                        {fieldname: 'category', fieldtype: 'Link', options: 'Document Category', label: __('Category'), default: doc.category || ''},
                        {fieldname: 'status', fieldtype: 'Select', label: __('Status'), options: 'Draft\nPublished\nObsolete', default: doc.status || 'Draft'},
                    ];
                    if (department_available.value) {
                        fields.push({fieldname: 'department', fieldtype: 'Link', options: 'Department', label: __('Department'), default: doc.department || ''});
                    }
                    fields.push(
                        {fieldname: 'expiry_date', fieldtype: 'Date', label: __('Expiry Date'), default: doc.expiry_date || ''},
                        {fieldname: 'review_date', fieldtype: 'Date', label: __('Review Date'), default: doc.review_date || ''},
                        {fieldname: 'description', fieldtype: 'Small Text', label: __('Description'), default: doc.description || ''},
                        {fieldname: 'only_me', fieldtype: 'Check', label: __('Only Me'), default: doc.only_me ? 1 : 0}
                    );
                    const hasTagOptions = Array.isArray(tags.value) && tags.value.length > 0;
                    if (hasTagOptions) {
                        fields.push({
                            fieldname: 'tags',
                            fieldtype: 'MultiCheck',
                            label: __('Tags'),
                            columns: 3,
                            options: tags.value.map((t) => ({
                                label: t.name,
                                value: t.name,
                                checked: currentTagSet.has(t.name),
                            })),
                        });
                    } else {
                        fields.push({fieldname: 'tags', fieldtype: 'Data', label: __('Tags (comma separated)'), default: Array.from(currentTagSet).join(', ')});
                    }
                    const dialog = new frappe.ui.Dialog({
                        title: __('Quick Edit'),
                        fields,
                        primary_action_label: __('Save'),
                        primary_action: (values) => {
                            const selectedTags = Array.isArray(values.tags) ? values.tags : csv(values.tags);
                            const args = {
                                doc_name: doc.name,
                                title: values.title,
                                document_code: values.document_code || '',
                                category: values.category || '',
                                status: values.status,
                                description: values.description || '',
                                only_me: values.only_me ? 1 : 0,
                                expiry_date: values.expiry_date || '',
                                review_date: values.review_date || '',
                                tags: JSON.stringify(selectedTags)
                            };
                            if (department_available.value) {
                                args.department = values.department || '';
                            }
                            frappe.call({
                                method: 'document_management.frappe_document_management.page.document_management_console.document_management_console.update_document_metadata',
                                args,
                                freeze: true,
                                callback: (r) => {
                                    if (!r.exc) {
                                        dialog.hide();
                                        frappe.show_alert({message: __('Document updated'), indicator: 'green'});
                                        fetch_documents(false);
                                    }
                                }
                            });
                        }
                    });
                    dialog.show();
                };

                const upload_new_version = (doc) => {
                    if (!doc) return;
                    const input = document.createElement('input');
                    input.type = 'file';
                    input.onchange = () => {
                        const file = input.files?.[0];
                        if (!file) return;
                        frappe.prompt(
                            [{fieldname: 'change_log', fieldtype: 'Small Text', label: __('Change Log / Notes'), default: 'Uploaded new version'}],
                            async (values) => {
                                try {
                                    await upload_document_file(
                                        file,
                                        'document_management.frappe_document_management.page.document_management_console.document_management_console.add_document_version',
                                        {
                                            doc_name: doc.name,
                                            folder: doc.folder || '',
                                            change_log: values.change_log || ''
                                        }
                                    );
                                    frappe.show_alert({message: __('New version uploaded'), indicator: 'green'});
                                    fetch_documents(false);
                                    fetch_versions(doc.name);
                                } catch (err) {
                                    frappe.msgprint(err?.message || __('File upload failed.'));
                                }
                            },
                            __('Upload New Version')
                        );
                    };
                    input.click();
                };

                const view_ocr_text = (doc) => {
                    if (!doc) return;
                    frappe.call({
                        method: 'document_management.frappe_document_management.page.document_management_console.document_management_console.get_document_ocr_text',
                        args: {doc_name: doc.name},
                        freeze: true,
                        callback: (r) => {
                            const data = r.message || {};
                            const text = data.ocr_content || '';
                            const dialog = new frappe.ui.Dialog({
                                title: `${__('View OCR Text')} · ${doc.document_name || doc.name}`,
                                size: 'large',
                                fields: [
                                    {
                                        fieldname: 'ocr_html',
                                        fieldtype: 'HTML',
                                        options: text
                                            ? `<pre style="max-height: 60vh; overflow: auto; white-space: pre-wrap; padding: 12px; background: var(--subtle-fg); border-radius: 6px;">${escape_html(text)}</pre>`
                                            : `<p class="text-muted">${__('No OCR text extracted yet.')}</p>`
                                    }
                                ],
                                primary_action_label: __('Copy'),
                                primary_action: () => {
                                    if (!text) return;
                                    navigator.clipboard?.writeText(text).then(() => {
                                        frappe.show_alert({message: __('Copied to clipboard'), indicator: 'green'});
                                    });
                                }
                            });
                            dialog.show();
                        }
                    });
                };

                function sync_selected_doc() {
                    if (!selected_doc.value) return;
                    const refreshed = documents.value.find(
                        (doc) => doc.name === selected_doc.value.name
                    );
                    if (!refreshed) {
                        select_doc(null);
                        return;
                    }
                    const current_file = selected_doc.value.original_file || selected_doc.value.document_file;
                    const refreshed_file = refreshed.original_file || refreshed.document_file;
                    selected_doc.value = {
                        ...refreshed,
                        search_terms: refreshed.search_terms || selected_doc.value.search_terms,
                        search_page: refreshed.search_page || selected_doc.value.search_page
                    };
                    if (current_file !== refreshed_file) {
                        select_doc(selected_doc.value);
                    }
                }

                const is_selected = (name) => selected_names.value.has(name);
                const toggle_selection = (name) => {
                    const next = new Set(selected_names.value);
                    next.has(name) ? next.delete(name) : next.add(name);
                    selected_names.value = next;
                };
                const toggle_all = () => {
                    selected_names.value = all_selected.value
                        ? new Set()
                        : new Set(documents.value.map((doc) => doc.name));
                };
                const clear_selection = () => {
                    selected_names.value = new Set();
                };
                const selected_list = () => Array.from(selected_names.value);

                const run_bulk_action = (method, args, message) => {
                    return new Promise((resolve, reject) => {
                        frappe.call({
                            method: `document_management.frappe_document_management.page.document_management_console.document_management_console.${method}`,
                            args,
                            freeze: true,
                            callback: (r) => {
                                if (!r.exc) {
                                    frappe.show_alert({message, indicator: 'green'});
                                    selected_doc.value = null;
                                    fetch_documents();
                                    resolve(r.message);
                                }
                            },
                            error: reject
                        });
                    });
                };

                const toggle_trash = () => {
                    trash_mode.value = !trash_mode.value;
                    selected_doc.value = null;
                    clear_selection();
                    fetch_documents(true);
                };

                const show_bulk_edit = () => {
                    const dialog = new frappe.ui.Dialog({
                        title: __('Edit selected documents'),
                        fields: [
                            {fieldname: 'status', fieldtype: 'Select', label: __('Status'), options: '\nDraft\nPublished\nObsolete'},
                            {fieldname: 'category', fieldtype: 'Link', options: 'Document Category', label: __('Category')},
                            {fieldname: 'add_tags', fieldtype: 'Data', label: __('Add tags (comma separated)')},
                            {fieldname: 'remove_tags', fieldtype: 'Data', label: __('Remove tags (comma separated)')}
                        ],
                        primary_action_label: __('Apply'),
                        primary_action: (values) => {
                            dialog.hide();
                            run_bulk_action('bulk_update_documents', {
                                documents: JSON.stringify(selected_list()),
                                status: values.status || null,
                                category: values.category || null,
                                add_tags: JSON.stringify(csv(values.add_tags)),
                                remove_tags: JSON.stringify(csv(values.remove_tags))
                            }, __('Documents updated'));
                        }
                    });
                    dialog.show();
                };

                const export_selected = () => {
                    frappe.call({
                        method: 'document_management.frappe_document_management.utils.archive_portability.export_document_archive',
                        args: {documents: JSON.stringify(selected_list())},
                        freeze: true,
                        callback: (r) => {
                            const url = r.message?.download_url || r.message?.file_url;
                            if (url) {
                                window.open(url, '_blank', 'noopener');
                            }
                        }
                    });
                };

                const import_archive = () => {
                    const input = document.createElement('input');
                    input.type = 'file';
                    input.accept = '.zip,application/zip';
                    input.onchange = async () => {
                        const file = input.files?.[0];
                        if (!file) return;
                        try {
                            const result = await upload_document_file(
                                file,
                                'document_management.frappe_document_management.utils.archive_portability.import_document_archive',
                                {}
                            );
                            frappe.show_alert({
                                message: __('Imported {0} documents', [result.count || 0]),
                                indicator: 'green'
                            });
                            fetch_documents();
                        } catch (error) {
                            frappe.msgprint(error.message || __('Archive import failed.'));
                        }
                    };
                    input.click();
                };

                const trash_selected = () => {
                    frappe.confirm(__('Move selected documents to trash?'), () =>
                        run_bulk_action('move_documents_to_trash', {
                            documents: JSON.stringify(selected_list())
                        }, __('Documents moved to trash'))
                    );
                };
                const restore_selected = () => {
                    run_bulk_action('restore_documents', {
                        documents: JSON.stringify(selected_list())
                    }, __('Documents restored'));
                };
                const purge_selected = () => {
                    frappe.confirm(__('Permanently delete selected documents and their versions?'), () =>
                        run_bulk_action('permanently_delete_documents', {
                            documents: JSON.stringify(selected_list())
                        }, __('Documents permanently deleted'))
                    );
                };
                const trash_one = (name) => {
                    selected_names.value = new Set([name]);
                    trash_selected();
                };
                const restore_one = (name) => {
                    selected_names.value = new Set([name]);
                    restore_selected();
                };
                const purge_one = (name) => {
                    selected_names.value = new Set([name]);
                    purge_selected();
                };
                const csv = (value) => (value || '')
                    .split(',')
                    .map((item) => item.trim())
                    .filter(Boolean);

                const open_doc = (name) => {
                    frappe.set_route('Form', 'Document', name);
                };

                const status_class = (status) => {
                    if (status === 'Published') return 'published';
                    if (status === 'Draft') return 'draft';
                    if (status === 'Obsolete') return 'obsolete';
                    return '';
                };

                const dm_helpers = (frappe.document_management && frappe.document_management.helpers) || window.DocumentManagementHelpers || {};
                const is_pdf = (file) => dm_helpers.is_pdf ? dm_helpers.is_pdf(file) : Boolean(file && file.toLowerCase().endsWith('.pdf'));
                const is_img = (file) => dm_helpers.is_img ? dm_helpers.is_img(file) : Boolean(file && file.match(/\.(jpeg|jpg|gif|png|webp)$/i) != null);
                const is_office = (file) => dm_helpers.is_office ? dm_helpers.is_office(file) : Boolean(file && file.match(/\.(doc|docx|ppt|pptx|xls|xlsx)$/i) != null);
                const is_txt = (file) => dm_helpers.is_txt ? dm_helpers.is_txt(file) : Boolean(file && file.toLowerCase().endsWith('.txt'));
                const is_md = (file, doc = null) => dm_helpers.is_md ? dm_helpers.is_md(file, doc) : Boolean((file && file.toLowerCase().endsWith('.md')) || (doc && doc.is_markdown));

                const escape_html = (value) => dm_helpers.escape_html
                    ? dm_helpers.escape_html(value)
                    : frappe.utils.escape_html(String(value || ''));
                const highlight_text = (value, terms) => dm_helpers.highlight_text
                    ? dm_helpers.highlight_text(value, terms)
                    : escape_html(value);
                const sanitize_html = (html) => dm_helpers.sanitize_html
                    ? dm_helpers.sanitize_html(html)
                    : (window.DOMPurify ? window.DOMPurify.sanitize(html || '') : escape_html(html));

                const render_markdown = (text) => {
                    if (!text) return '';
                    try {
                        if (window.marked && typeof window.marked.parse === 'function') {
                            const parsed = sanitize_html(window.marked.parse(text));
                            const parsed_doc = new DOMParser().parseFromString(parsed, 'text/html');
                            const slug_counts = new Map();
                            parsed_doc.querySelectorAll('h1, h2, h3, h4, h5, h6').forEach((heading) => {
                                const base_slug = markdown_anchor_slug(heading.textContent);
                                if (!base_slug) return;
                                const count = slug_counts.get(base_slug) || 0;
                                heading.id = count ? `${base_slug}-${count}` : base_slug;
                                slug_counts.set(base_slug, count + 1);
                            });
                            return parsed_doc.body.innerHTML;
                        }
                    } catch (e) {
                        console.error('Error parsing markdown', e);
                    }
                    return `<pre style="white-space: pre-wrap;">${escape_html(text)}</pre>`;
                };

                const handle_markdown_click = (event) => {
                    const link = event.target.closest('a[href^="#"]');
                    if (!link || !event.currentTarget.contains(link)) return;

                    event.preventDefault();
                    event.stopPropagation();

                    let target_id;
                    try {
                        target_id = decodeURIComponent(link.getAttribute('href').slice(1));
                    } catch (e) {
                        target_id = link.getAttribute('href').slice(1);
                    }
                    const target = Array.from(event.currentTarget.querySelectorAll('[id]'))
                        .find((element) => element.id === target_id);
                    if (!target) return;

                    target.scrollIntoView({behavior: 'smooth', block: 'start'});
                };

                const get_file_type = (doc) => dm_helpers.get_file_type ? dm_helpers.get_file_type(doc) : 'generic';
                const get_icon_class = (doc) => get_file_type(doc);
                const get_icon = (doc) => dm_helpers.get_icon ? dm_helpers.get_icon(doc) : 'FILE';
                const get_tag_style = (color) => dm_helpers.get_tag_style
                    ? dm_helpers.get_tag_style(color)
                    : {'--tag-color': '#e2e8f0', '--tag-contrast': '#000000'};

                const is_fullscreen = ref(false);

                const copy_link = (doc) => {
                    if (!doc) return;
                    const params = { Doc: doc.name };
                    if (doc.search_page) params.page = doc.search_page;
                    const link = window.location.origin + frappe.document_management.helpers.desk_page_url('document-management-console', params);

                    navigator.clipboard.writeText(link).then(() => {
                        frappe.show_alert({message: __('Link copied to clipboard!'), indicator: 'green'});
                    }).catch(err => {
                        console.error('Failed to copy link', err);
                        frappe.msgprint(__('Unable to copy link to clipboard.'));
                    });
                };

                const manage_share_links = (doc) => {
                    if (!doc || (provider && !adapter?.share_methods)) return;
                    const share_methods = adapter?.share_methods;
                    const dialog = new frappe.ui.Dialog({
                        title: __('External share links'),
                        fields: [
                            {
                                fieldname: 'expiration_days',
                                fieldtype: 'Int',
                                label: __('Expiration (days)'),
                                default: 7,
                                reqd: 1
                            },
                            {
                                fieldname: 'file_version',
                                fieldtype: 'Select',
                                label: __('File version'),
                                options: 'Original\nSearchable PDF',
                                default: share_methods ? 'Searchable PDF' : 'Original',
                                reqd: 1
                            },
                            {
                                fieldname: 'hide_download_button',
                                fieldtype: 'Check',
                                label: __('Hide download button')
                            },
                            {
                                fieldname: 'max_views',
                                fieldtype: 'Int',
                                non_negative: 1,
                                label: __('Maximum views (0 = unlimited)'),
                                default: 0
                            },
                            {
                                fieldname: 'passcode',
                                fieldtype: 'Password',
                                label: __('Passcode (optional, min 4 chars)')
                            },
                            {
                                fieldname: 'links_html',
                                fieldtype: 'HTML',
                                label: __('Existing links')
                            }
                        ],
                        primary_action_label: __('Create and copy link'),
                        primary_action: (values) => {
                            frappe.call({
                                method: share_methods?.create || 'document_management.frappe_document_management.utils.document_sharing.create_share_link',
                                args: {
                                    document: doc.name,
                                    ...(share_methods ? { revision: doc.revision } : {}),
                                    expiration_days: values.expiration_days,
                                    file_version: values.file_version,
                                    hide_download_button: values.hide_download_button ? 1 : 0,
                                    max_views: values.max_views || 0,
                                    passcode: values.passcode || null
                                },
                                callback: (r) => {
                                    if (!r.exc && r.message?.url) {
                                        navigator.clipboard.writeText(r.message.url);
                                        frappe.show_alert({
                                            message: __('Share link copied to clipboard'),
                                            indicator: 'green'
                                        });
                                        load_links();
                                    }
                                }
                            });
                        }
                    });
                    const load_links = () => {
                        frappe.call({
                            method: share_methods?.list || 'document_management.frappe_document_management.utils.document_sharing.get_share_links',
                            args: {document: doc.name},
                            callback: (r) => {
                                const links = r.message || [];
                                const wrapper = dialog.fields_dict.links_html.$wrapper;
                                if (!links.length) {
                                    wrapper.html(`<p class="text-muted">${__('No share links created.')}</p>`);
                                    return;
                                }
                                wrapper.html(links.map((link) => {
                                    const views = Number(link.access_count || 0);
                                    const maxV = Number(link.max_views || 0);
                                    const viewsLabel = maxV > 0 ? `${views}/${maxV}` : `${views}`;
                                    const lockBadge = link.has_passcode ? ' · <i class="fa fa-lock"></i>' : '';
                                    return `
                                    <div class="share-link-row" style="display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid var(--border-color);">
                                        <span>${frappe.utils.escape_html(link.file_version)} · ${frappe.datetime.str_to_user(link.expires_at)} · ${viewsLabel} ${__('views')}${lockBadge} · ${link.enabled ? __('Active') : __('Revoked')}</span>
                                        ${link.enabled ? `<button class="btn btn-xs btn-danger revoke-share-link" data-name="${frappe.utils.escape_html(link.name)}">${__('Revoke')}</button>` : ''}
                                    </div>
                                `;
                                }).join(''));
                                wrapper.find('.revoke-share-link').on('click', function() {
                                    frappe.call({
                                        method: share_methods?.revoke || 'document_management.frappe_document_management.utils.document_sharing.revoke_share_link',
                                        args: {name: this.dataset.name},
                                        callback: load_links
                                    });
                                });
                            }
                        });
                    };
                    dialog.show();
                    load_links();
                };

                const force_pdf = (doc_name) => {
                    frappe.call({
                        method: 'document_management.frappe_document_management.page.document_management_console.document_management_console.force_generate_pdf',
                        args: { doc_name: doc_name },
                        callback: function(r) {
                            if (!r.exc) {
                                frappe.show_alert({
                                    message: r.message || __('PDF generated successfully'),
                                    indicator: 'green'
                                });
                                setTimeout(() => {
                                    fetch_documents();
                                }, 1500);
                            }
                        }
                    });
                };

                const reprocess_ocr = (doc_name) => {
                    frappe.confirm(
                        __('Reprocess OCR from the original file and rebuild page-level text?'),
                        () => {
                            frappe.call({
                                method: 'document_management.frappe_document_management.page.document_management_console.document_management_console.reprocess_ocr',
                                args: {doc_name},
                                callback: (r) => {
                                    if (!r.exc) {
                                        selected_doc.value.ocr_status = 'Pending';
                                        frappe.show_alert({
                                            message: __('OCR reprocessing queued'),
                                            indicator: 'blue'
                                        });
                                    }
                                }
                            });
                        }
                    );
                };

                const format_date = (date_str) => {
                    if (!date_str) return '';
                    return frappe.datetime.str_to_user(date_str.split(' ')[0]);
                };

                const open_party = (party_type, party_name) => {
                    frappe.set_route('Form', party_type, party_name);
                };

                const fetch_folders = () => {
                    frappe.call({
                        method: 'document_management.frappe_document_management.page.document_management_console.document_management_console.get_folders',
                        callback: (r) => {
                            folders.value = r.message || [];
                        }
                    });
                };

                const fetch_tags = () => {
                    frappe.call({
                        method: 'document_management.frappe_document_management.page.document_management_console.document_management_console.get_tags',
                        callback: (r) => {
                            tags.value = r.message || [];
                        }
                    });
                };

                const fetch_access_capabilities = () => {
                    frappe.call({
                        method: 'document_management.frappe_document_management.utils.document_access.get_document_access_capabilities',
                        callback: (r) => {
                            const capabilities = r.message || {};
                            department_available.value = Boolean(capabilities.department);
                        },
                        error: () => {
                            department_available.value = false;
                        }
                    });
                };

                let page_visible = true;
                let needs_refresh = false;

                const is_console_active = () => {
                    if (document.hidden || !page_visible) return false;
                    const route = frappe.get_route ? frappe.get_route() : [];
                    return !route.length || route[0] === 'document-management-console';
                };

                const handle_realtime_document_change = (event) => {
                    if (!event || event.doctype !== 'Document') return;
                    if (!is_console_active()) {
                        needs_refresh = true;
                        return;
                    }
                    clearTimeout(realtimeRefreshTimer);
                    realtimeRefreshTimer = setTimeout(() => {
                        fetch_categories();
                        fetch_tags();
                        fetch_documents(false);
                    }, 250);
                };

                const handle_keydown = (event) => {
                    if (event.key === 'Escape' && is_fullscreen.value) {
                        is_fullscreen.value = false;
                    }
                };

                const adapter = provider?.setup({
                    Vue, documents, categories, tags, selected_doc, loading, filters, view_mode, is_fullscreen,
                    manage_share_links, sort_by, current_page, total_documents, has_more, page_size
                });
                const extensions = adapter?.components || {};
                const hide_empty_state = adapter?.hide_empty_state || false;
                const sidebar_class = adapter?.sidebar_class || '';
                const sort_disabled = adapter?.sort_disabled || computed(() => Boolean(filters.search && !trash_mode.value));

                const console_instance = console_controller;
                console_instance.on_page_show = () => {
                    page_visible = true;
                    if (adapter) return adapter.on_show();
                    if (needs_refresh) {
                        needs_refresh = false;
                        fetch_categories();
                        fetch_tags();
                        fetch_documents(false);
                    }
                };
                console_instance.on_page_hide = () => {
                    page_visible = false;
                };

                onMounted(() => {
                    window.addEventListener('click', handle_window_click);
                    window.addEventListener('keydown', handle_keydown);
                    if (adapter) {
                        adapter.on_show();
                        return;
                    }
                    fetch_access_capabilities();
                    fetch_categories();
                    fetch_folders();
                    fetch_saved_views();
                    fetch_tags();
                    fetch_documents();
                    if (frappe.realtime && frappe.realtime.on) {
                        frappe.realtime.on(realtime_event, handle_realtime_document_change);
                    }
                });

                onBeforeUnmount(() => {
                    clearTimeout(debounceTimer);
                    clearTimeout(realtimeRefreshTimer);
                    if (!adapter && frappe.realtime && frappe.realtime.off) {
                        frappe.realtime.off(realtime_event);
                    }
                    window.removeEventListener('click', handle_window_click);
                    window.removeEventListener('keydown', handle_keydown);
                });

                return {
                    provider, extensions, hide_empty_state, sidebar_class, sort_disabled,
                    documents, current_page, total_documents, total_pages, has_more, sort_by, go_to_page, show_search_help,
                    categories, folders, tags, selected_doc, selected_versions, preview_version, quick_edit_doc, upload_new_version, view_ocr_text,
                    view_mode, loading, filters, is_fullscreen,
                    department_available,
                    trash_mode, selected_count, all_selected, can_purge,
                    saved_views, selected_saved_view,
                    show_category_dropdown, toggle_category_dropdown, clear_categories,
                    show_status_dropdown, toggle_status_dropdown, clear_statuses,
                    show_tag_dropdown, toggle_tag_dropdown, clear_tags,
                    fetch_documents, debounce_fetch, select_doc, open_doc, status_class, force_pdf,
                    apply_saved_view, save_view, delete_view,
                    reprocess_ocr, copy_link, manage_share_links,
                    is_selected, toggle_selection, toggle_all, clear_selection, toggle_trash,
                    show_bulk_edit, trash_selected, restore_selected, purge_selected,
                    export_selected, import_archive,
                    trash_one, restore_one, purge_one,
                    is_pdf, is_img, is_office, is_txt, is_md, text_preview_content, loading_text_preview, highlight_text, render_markdown, handle_markdown_click,
                    get_icon, get_icon_class, get_file_type, get_tag_style,
                    format_date, open_party,
                    show_upload_modal, upload_form, dragover, is_uploading, close_upload_modal, handle_drop, handle_file_select, remove_file, submit_upload
                };
            }
        });

        app.component(
            'loan-pdf-viewer',
            window.LoanManagerPdfSearchViewer.createVueComponent(Vue)
        );
        app.mount('#document-management-vue-app');
    }
}
