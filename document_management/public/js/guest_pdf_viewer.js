(() => {
    window.addEventListener("message", (event) => {
        if (event.source !== window.parent || !(event.data instanceof ArrayBuffer)) {
            return;
        }

        const pages = document.getElementById("pdf-pages");
        const pageCount = document.getElementById("pdf-page-count");
        const matchCount = document.getElementById("pdf-match-count");
        const errorMessage = document.getElementById("pdf-error");
        const source = URL.createObjectURL(new Blob([event.data], {type: "application/pdf"}));

        const reportReady = () => window.parent.postMessage("document-share-pdf-ready", "*");
        const showError = () => {
            window.parent.postMessage("document-share-pdf-error", "*");
            pages.hidden = true;
            errorMessage.hidden = false;
        };
        const PdfSearchViewer = window.LoanManagerPdfSearchViewer?.PdfSearchViewer;
        if (!PdfSearchViewer) {
            showError();
            return;
        }
        const viewer = new PdfSearchViewer(pages, {
            source,
            onPageChange(current, total) {
                pageCount.textContent = current + " / " + total;
            },
            onMatchesChange(current, total) {
                matchCount.textContent = total ? current + " / " + total : "";
            },
            onError: showError,
        });

        const pageRendered = ({error}) => {
            if (error) {
                showError();
                return;
            }
            viewer.eventBus.off("pagerendered", pageRendered);
            reportReady();
        };
        viewer.eventBus.on("pagerendered", pageRendered);
        document.getElementById("pdf-previous").onclick = () => viewer.setPage(viewer.viewer.currentPageNumber - 1);
        document.getElementById("pdf-next").onclick = () => viewer.setPage(viewer.viewer.currentPageNumber + 1);
        document.getElementById("pdf-zoom-in").onclick = () => viewer.zoomIn();
        document.getElementById("pdf-zoom-out").onclick = () => viewer.zoomOut();
        document.getElementById("pdf-search").oninput = (input) => viewer.setQuery(input.target.value);
        document.getElementById("pdf-find-next").onclick = () => viewer.nextMatch();
        pages.addEventListener("click", (click) => {
            if (click.target.closest(".annotationLayer a")) {
                click.preventDefault();
            }
        }, true);
        window.addEventListener("pagehide", () => {
            viewer.destroy();
            URL.revokeObjectURL(source);
        }, {once: true});
    }, {once: true});
})();
