import {
    getDocument,
} from "pdfjs-dist/legacy/build/pdf.mjs";
import {
    WorkerMessageHandler,
} from "pdfjs-dist/legacy/build/pdf.worker.mjs";
import {
    EventBus,
    PDFFindController,
    PDFLinkService,
    PDFViewer,
} from "pdfjs-dist/web/pdf_viewer.mjs";


const VIEWER_VERSION = "5.7.284-10";

// Keep the worker implementation in this bundle so deployments do not depend
// on web-server MIME configuration for PDF.js module workers.
globalThis.pdfjsWorker = {WorkerMessageHandler};


class PdfSearchViewer {
    constructor(container, options = {}) {
        this.container = container;
        this.source = options.source || "";
        this.query = this.normalizeQuery(options.query);
        this.initialPage = Number(options.page) || 1;
        this.loadingTask = null;
        this.pdf = null;
        this.lastFindKey = null;
        this.fallbackPages = new Set();
        this.fallbackImageCache = new Map();
        this.fallbackTimers = new Map();
        this.usesServerFallback = false;
        this.prefetchTimer = null;
        this.resizeTimer = null;
        this.fitToWidth = false;

        this.maxScale = Number(options.maxScale) || 1.5;

        this.eventBus = new EventBus();
        this.linkService = new PDFLinkService({eventBus: this.eventBus});
        this.findController = new PDFFindController({
            eventBus: this.eventBus,
            linkService: this.linkService,
            updateMatchesCountOnProgress: true,
        });

        this.viewerElement = document.createElement("div");
        this.viewerElement.className = "pdfViewer";
        this.container.replaceChildren(this.viewerElement);
        this.viewer = new PDFViewer({
            container: this.container,
            viewer: this.viewerElement,
            eventBus: this.eventBus,
            linkService: this.linkService,
            findController: this.findController,
            textLayerMode: 1,
            removePageBorders: true,
        });
        this.linkService.setViewer(this.viewer);

        this.onPagesInit = () => {
            this.viewer.currentScale = 1;
            this.goToPage(this.initialPage);
            options.onPageChange?.(
                this.viewer.currentPageNumber,
                this.viewer.pagesCount,
            );
            this.dispatchFind(true);
            this.scheduleNeighborPrefetch(this.viewer.currentPageNumber);
        };
        this.onPageRendered = ({pageNumber}) => {
            if (this.usesServerFallback) {
                this.renderFallbackPage(pageNumber);
            } else {
                this.scheduleFallbackCheck(pageNumber);
            }
        };
        this.onPageChanging = ({pageNumber}) => {
            options.onPageChange?.(pageNumber, this.viewer.pagesCount);
            this.scheduleNeighborPrefetch(pageNumber);
            if (this.usesServerFallback) {
                window.setTimeout(() => this.renderFallbackPage(pageNumber), 0);
            }
        };
        this.onMatchesCount = ({matchesCount}) => {
            options.onMatchesChange?.(
                matchesCount?.current || 0,
                matchesCount?.total || 0,
            );
        };
        this.onScaleChanging = ({scale}) => {
            options.onScaleChange?.(Math.round((scale || 1) * 100));
        };
        this.eventBus.on("pagesinit", this.onPagesInit);
        this.eventBus.on("pagerendered", this.onPageRendered);
        this.eventBus.on("pagechanging", this.onPageChanging);
        this.eventBus.on("updatefindmatchescount", this.onMatchesCount);
        this.eventBus.on("updatefindcontrolstate", this.onMatchesCount);
        this.eventBus.on("scalechanging", this.onScaleChanging);

        this.resizeObserver = new ResizeObserver(() => {
            if (!this.fitToWidth) {
                return;
            }
            window.clearTimeout(this.resizeTimer);
            this.resizeTimer = window.setTimeout(
                () => this.fitWidthToContainer(),
                120,
            );
        });
        this.resizeObserver.observe(this.container);
        this.load().catch((error) => options.onError?.(error));
    }

    async load() {
        if (!this.source) {
            return;
        }
        this.loadingTask = getDocument({
            url: this.source,
            withCredentials: true,
        });
        this.pdf = await this.loadingTask.promise;
        this.linkService.setDocument(this.pdf);
        this.viewer.setDocument(this.pdf);
    }

    fitWidthToContainer() {
        if (!this.viewer.pagesCount) {
            return;
        }
        this.viewer.currentScaleValue = "page-width";
        if (this.viewer.currentScale > this.maxScale) {
            this.viewer.currentScale = this.maxScale;
        }
    }

    getFallbackUrl(pageNumber) {
        try {
            const url = new URL(this.source, window.location.origin);
            const documentName = url.searchParams.get("document");
            if (!documentName) {
                return "";
            }
            const fileVersion = url.searchParams.get("file_version")
                || "Searchable PDF";
            const versionName = url.searchParams.get("version_name");
            const fallback = new URL(
                "/api/method/document_management.frappe_document_management.page.document_management_console.document_management_console.render_document_pdf_page",
                window.location.origin,
            );
            fallback.searchParams.set("document", documentName);
            fallback.searchParams.set("file_version", fileVersion);
            if (versionName) {
                fallback.searchParams.set("version_name", versionName);
            }
            fallback.searchParams.set("page", pageNumber);
            fallback.searchParams.set("dpi", this.fallbackDpi());
            return fallback.toString();
        } catch {
            return "";
        }
    }

    fallbackDpi() {
        const scale = Number(this.viewer?.currentScale) || 1;
        const pixelRatio = Math.min(Number(window.devicePixelRatio) || 1, 2);
        return Math.min(Math.max(Math.ceil(72 * scale * pixelRatio), 144), 300);
    }

    isCanvasBlank(canvas) {
        const context = canvas?.getContext?.("2d", {willReadFrequently: true});
        if (!context || !canvas.width || !canvas.height) {
            return false;
        }
        const sampleSize = 24;
        const stepX = Math.max(1, Math.floor(canvas.width / sampleSize));
        const stepY = Math.max(1, Math.floor(canvas.height / sampleSize));
        let samples = 0;
        let nonWhite = 0;
        for (let y = Math.floor(stepY / 2); y < canvas.height; y += stepY) {
            for (let x = Math.floor(stepX / 2); x < canvas.width; x += stepX) {
                const data = context.getImageData(x, y, 1, 1).data;
                samples += 1;
                if (data[3] > 8 && (data[0] < 245 || data[1] < 245 || data[2] < 245)) {
                    nonWhite += 1;
                }
            }
        }
        return samples > 0 && (nonWhite / samples) < 0.002;
    }

    scheduleFallbackCheck(pageNumber) {
        if (
            this.fallbackPages.has(pageNumber)
            || this.fallbackTimers.has(pageNumber)
        ) {
            return;
        }
        const fallbackUrl = this.getFallbackUrl(pageNumber);
        if (!fallbackUrl) {
            return;
        }
        const timer = window.setTimeout(
            () => {
                this.fallbackTimers.delete(pageNumber);
                this.renderFallbackIfBlank(pageNumber, fallbackUrl);
            },
            20,
        );
        this.fallbackTimers.set(pageNumber, timer);
    }

    prefetchFallbackPages(pageNumber) {
        if (!this.viewer.pagesCount) {
            return;
        }
        const MAX_FALLBACK_CACHE = 20;
        [
            Number(pageNumber),
            Number(pageNumber) + 1,
            Number(pageNumber) - 1,
        ]
            .filter((page) => page >= 1 && page <= this.viewer.pagesCount)
            .forEach((page) => {
                const url = this.getFallbackUrl(page);
                if (!url) {
                    return;
                }
                if (this.fallbackImageCache.has(url)) {
                    // Refresh LRU position
                    const existing = this.fallbackImageCache.get(url);
                    this.fallbackImageCache.delete(url);
                    this.fallbackImageCache.set(url, existing);
                    return;
                }
                const image = new Image();
                image.decoding = "async";
                image.src = url;
                this.fallbackImageCache.set(url, image);
                while (this.fallbackImageCache.size > MAX_FALLBACK_CACHE) {
                    const oldestKey = this.fallbackImageCache.keys().next().value;
                    this.fallbackImageCache.delete(oldestKey);
                }
            });
    }

    renderFallbackPage(pageNumber) {
        const fallbackUrl = this.getFallbackUrl(pageNumber);
        if (!fallbackUrl || this.fallbackPages.has(pageNumber)) {
            return;
        }
        const page = this.viewerElement.querySelector(
            `.page[data-page-number="${pageNumber}"]`,
        );
        const wrapper = page?.querySelector(".canvasWrapper");
        if (!wrapper) {
            return;
        }
        this.fallbackPages.add(pageNumber);
        const cached = this.fallbackImageCache.get(fallbackUrl);
        const image = cached?.cloneNode(false) || document.createElement("img");
        image.className = "loan-pdf-page-fallback";
        image.alt = "";
        image.src = fallbackUrl;
        wrapper.append(image);
    }

    renderFallbackIfBlank(pageNumber, fallbackUrl) {
        const page = this.viewerElement.querySelector(
            `.page[data-page-number="${pageNumber}"]`,
        );
        const canvas = page?.querySelector(".canvasWrapper canvas");
        if (!page || !canvas || !this.isCanvasBlank(canvas)) {
            return;
        }
        if (this.fallbackPages.has(pageNumber)) {
            return;
        }
        this.usesServerFallback = true;
        this.prefetchFallbackPages(pageNumber);
        this.renderFallbackPage(pageNumber);
    }

    scheduleNeighborPrefetch(pageNumber) {
        if (!this.pdf || !this.viewer.pagesCount) {
            return;
        }
        window.clearTimeout(this.prefetchTimer);
        this.prefetchTimer = window.setTimeout(() => {
            const pages = [
                Number(pageNumber) + 1,
                Number(pageNumber) - 1,
            ].filter((page) => page >= 1 && page <= this.viewer.pagesCount);
            pages.forEach((page) => {
                this.pdf.getPage(page).catch(() => null);
            });
            if (this.usesServerFallback) {
                this.prefetchFallbackPages(pageNumber);
            }
        }, 160);
    }

    setQuery(query) {
        const normalized = this.normalizeQuery(query);
        if (JSON.stringify(normalized) === JSON.stringify(this.query)) {
            return;
        }
        this.query = normalized;
        this.lastFindKey = null;
        this.dispatchFind(true);
    }

    setPage(page) {
        this.initialPage = Number(page) || 1;
        this.goToPage(this.initialPage);
    }

    goToPage(page) {
        if (!this.viewer.pagesCount) {
            return;
        }
        this.viewer.currentPageNumber = Math.min(
            Math.max(Math.trunc(Number(page) || 1), 1),
            this.viewer.pagesCount,
        );
    }

    nextMatch(previous = false) {
        if (!this.hasQuery()) {
            return;
        }
        this.eventBus.dispatch("find", this.findOptions("again", previous));
    }

    zoomIn() {
        this.fitToWidth = false;
        this.viewer.increaseScale({steps: 1});
    }

    zoomOut() {
        this.fitToWidth = false;
        this.viewer.decreaseScale({steps: 1});
    }

    fitWidth() {
        this.fitToWidth = true;
        this.fitWidthToContainer();
    }

    rotateClockwise() {
        if (!this.viewer.pagesCount) {
            return;
        }
        this.viewer.pagesRotation = ((this.viewer.pagesRotation || 0) + 90) % 360;
    }

    dispatchFind(force) {
        if (!this.viewer.pagesCount) {
            return;
        }
        const findKey = this.queryKey(this.query);
        if (!force && findKey === this.lastFindKey) {
            return;
        }
        this.lastFindKey = findKey;
        this.eventBus.dispatch("find", this.findOptions("", false));
    }

    queryKey(query) {
        return Array.isArray(query) ? JSON.stringify(query) : String(query || "");
    }

    findOptions(type, findPrevious) {
        return {
            source: this,
            type,
            query: this.query,
            caseSensitive: false,
            entireWord: false,
            findPrevious,
            highlightAll: this.hasQuery(),
            matchDiacritics: false,
            phraseSearch: true,
        };
    }

    normalizeQuery(query) {
        if (Array.isArray(query)) {
            return Array.from(
                new Set(query.map((term) => String(term || "").trim()).filter(Boolean))
            );
        }
        return String(query || "").trim();
    }

    hasQuery() {
        return Array.isArray(this.query)
            ? this.query.length > 0
            : Boolean(this.query);
    }

    destroy() {
        window.clearTimeout(this.resizeTimer);
        window.clearTimeout(this.prefetchTimer);
        this.fallbackTimers.forEach((timer) => window.clearTimeout(timer));
        this.fallbackTimers.clear();
        this.fallbackImageCache.clear();
        this.resizeObserver?.disconnect();
        this.eventBus.off("pagesinit", this.onPagesInit);
        this.eventBus.off("pagerendered", this.onPageRendered);
        this.eventBus.off("pagechanging", this.onPageChanging);
        this.eventBus.off("updatefindmatchescount", this.onMatchesCount);
        this.eventBus.off("updatefindcontrolstate", this.onMatchesCount);
        this.eventBus.off("scalechanging", this.onScaleChanging);
        this.loadingTask?.destroy();
        this.viewer?.cleanup();
        this.container.replaceChildren();
    }
}


function createVueComponent(Vue) {
    return {
        props: {
            src: {type: String, required: true},
            query: {type: String, default: ""},
            terms: {type: Array, default: () => []},
            page: {type: Number, default: 1},
            maxScale: {type: Number, default: 1.5},
        },
        setup(props) {
            const root = Vue.ref(null);
            const searchInput = Vue.ref(null);
            const viewer = Vue.shallowRef(null);
            const currentPage = Vue.ref(props.page || 1);
            const pageInput = Vue.ref(String(props.page || 1));
            const pageCount = Vue.ref(0);
            const currentMatch = Vue.ref(0);
            const totalMatches = Vue.ref(0);
            const scalePercent = Vue.ref(100);
            const error = Vue.ref("");
            const initialQuery = () => props.terms.length
                ? props.terms.join(" ")
                : props.query;
            const searchQuery = Vue.ref(initialQuery());
            let searchTimer = 0;

            const onKeydown = (event) => {
                if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") {
                    if (searchInput.value) {
                        event.preventDefault();
                        searchInput.value.focus();
                        searchInput.value.select();
                    }
                }
            };

            Vue.onMounted(() => {
                viewer.value = new PdfSearchViewer(root.value, {
                    source: props.src,
                    query: props.terms.length ? props.terms : props.query,
                    page: props.page,
                    maxScale: props.maxScale,
                    onPageChange: (current, total) => {
                        currentPage.value = current;
                        pageInput.value = String(current);
                        pageCount.value = total;
                    },
                    onMatchesChange: (current, total) => {
                        currentMatch.value = current;
                        totalMatches.value = total;
                    },
                    onScaleChange: (percent) => {
                        scalePercent.value = percent;
                    },
                    onError: (loadError) => {
                        console.error("Unable to load PDF preview", loadError);
                        const detail = loadError?.message || String(loadError || "");
                        error.value = detail
                            ? `${__("Unable to load PDF preview.")} ${detail}`
                            : __("Unable to load PDF preview.");
                    },
                });
            });
            Vue.watch(
                () => [props.query, props.terms],
                () => {
                    searchQuery.value = initialQuery();
                    viewer.value?.setQuery(
                        props.terms.length ? props.terms : props.query
                    );
                },
                {deep: true},
            );
            Vue.watch(() => props.page, (value) => viewer.value?.setPage(value));
            Vue.onBeforeUnmount(() => {
                window.clearTimeout(searchTimer);
                viewer.value?.destroy();
            });

            const runSearch = () => {
                window.clearTimeout(searchTimer);
                viewer.value?.setQuery(searchQuery.value);
            };
            const runSearchAndNext = (event) => {
                runSearch();
                viewer.value?.nextMatch(Boolean(event?.shiftKey));
            };
            const scheduleSearch = () => {
                window.clearTimeout(searchTimer);
                searchTimer = window.setTimeout(runSearch, 220);
            };
            const jumpToPage = () => {
                const parsed = parseInt(pageInput.value, 10);
                if (!Number.isNaN(parsed) && parsed >= 1) {
                    viewer.value?.goToPage(parsed);
                } else {
                    pageInput.value = String(currentPage.value);
                }
            };

            return {
                root,
                searchInput,
                currentPage,
                pageInput,
                pageCount,
                currentMatch,
                totalMatches,
                scalePercent,
                searchQuery,
                error,
                onKeydown,
                jumpToPage,
                hasSearch: Vue.computed(
                    () => Boolean(searchQuery.value.trim())
                ),
                runSearch,
                runSearchAndNext,
                scheduleSearch,
                clearSearch: () => {
                    window.clearTimeout(searchTimer);
                    searchQuery.value = "";
                    viewer.value?.setQuery("");
                },
                previousMatch: () => viewer.value?.nextMatch(true),
                nextMatch: () => viewer.value?.nextMatch(false),
                previousPage: () => viewer.value?.goToPage(currentPage.value - 1),
                nextPage: () => viewer.value?.goToPage(currentPage.value + 1),
                zoomOut: () => viewer.value?.zoomOut(),
                zoomIn: () => viewer.value?.zoomIn(),
                fitWidth: () => viewer.value?.fitWidth(),
                rotateClockwise: () => viewer.value?.rotateClockwise(),
            };
        },
        template: `
            <div class="loan-pdf-shell" tabindex="-1" @keydown="onKeydown">
                <div class="loan-pdf-toolbar">
                    <button type="button" @click="previousPage" :disabled="currentPage <= 1"
                        title="${__("Previous page")}">
                        <i class="fa fa-chevron-left"></i>
                    </button>
                    <input v-model="pageInput" type="number" min="1" :max="pageCount || 1"
                        class="loan-pdf-page-input"
                        style="width:44px;height:24px;text-align:center;border:1px solid rgba(255,255,255,0.25);border-radius:4px;background:rgba(255,255,255,0.12);color:#fff;font-size:12px;"
                        title="${__("Go to page")}"
                        @keyup.enter="jumpToPage" @blur="jumpToPage">
                    <span>/ {{ pageCount || "..." }}</span>
                    <button type="button" @click="nextPage"
                        :disabled="!pageCount || currentPage >= pageCount"
                        title="${__("Next page")}">
                        <i class="fa fa-chevron-right"></i>
                    </button>
                    <span class="loan-pdf-toolbar-divider"></span>
                    <button type="button" @click="zoomOut" title="${__("Zoom out")}">
                        <i class="fa fa-search-minus"></i>
                    </button>
                    <button type="button" class="loan-pdf-scale" @click="fitWidth"
                        title="${__("Fit to width")}">
                        {{ scalePercent }}%
                    </button>
                    <button type="button" @click="zoomIn" title="${__("Zoom in")}">
                        <i class="fa fa-search-plus"></i>
                    </button>
                    <button type="button" @click="rotateClockwise" title="${__("Rotate clockwise")}">
                        <i class="fa fa-rotate-right"></i>
                    </button>
                    <div class="loan-pdf-search">
                        <i class="fa fa-search"></i>
                        <input ref="searchInput" v-model="searchQuery" type="search"
                            placeholder="${__("Search in document")}"
                            @input="scheduleSearch" @keyup.enter="runSearchAndNext">
                        <button v-if="hasSearch" type="button" class="loan-pdf-clear"
                            @click="clearSearch" title="${__("Clear search")}">
                            <i class="fa fa-times"></i>
                        </button>
                    </div>
                    <span v-if="hasSearch" class="loan-pdf-match-count">
                        {{ currentMatch }} / {{ totalMatches }}
                    </span>
                    <button v-if="hasSearch" type="button" @click="previousMatch"
                        title="${__("Previous match")}">
                        <i class="fa fa-arrow-up"></i>
                    </button>
                    <button v-if="hasSearch" type="button" @click="nextMatch"
                        title="${__("Next match")}">
                        <i class="fa fa-arrow-down"></i>
                    </button>
                </div>
                <div class="loan-pdf-viewport">
                    <div v-if="error" class="loan-pdf-error">{{ error }}</div>
                    <div v-show="!error" ref="root" class="loan-pdf-container"></div>
                </div>
            </div>
        `,
    };
}


window.LoanManagerPdfSearchViewer = {
    version: VIEWER_VERSION,
    PdfSearchViewer,
    createVueComponent,
};
window.DocumentManagementPdfViewer = window.LoanManagerPdfSearchViewer;
