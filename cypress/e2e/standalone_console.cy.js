const consolePath = "/app/document-management-console";
const method = (name) => "**/api/method/**document_management_console." + name;

function openUpload() {
	cy.get(".paperless-toolbar .btn-upload").click();
	cy.get(".pc-modal-overlay .pc-modal").should("be.visible");
}

function assertUploadControls() {
	for (const selector of [".pc-modal-header", ".pc-modal-footer"]) {
		cy.get(selector).should("be.visible").and(($element) => {
			const rect = $element[0].getBoundingClientRect();
			expect(rect.top, selector + " top inside viewport").to.be.at.least(0);
			expect(rect.bottom, selector + " bottom inside viewport")
				.to.be.at.most($element[0].ownerDocument.defaultView.innerHeight);
		});
	}
}

describe("Standalone document console", () => {
	beforeEach(() => {
		const session = Cypress.env("documentManagementSession");
		if (session) {
			cy.setCookie("sid", session, { log: false });
		} else {
			cy.request({
				method: "POST", url: "/api/method/login", log: false,
				body: { usr: "Administrator", pwd: Cypress.env("adminPassword") },
			});
		}
		cy.intercept(method("get_documents")).as("documents");
	});

	it("lists documents and exposes the native filters and actions without a provider", () => {
		cy.viewport(1440, 660);
		cy.visit(consolePath);
		cy.wait("@documents").its("response.statusCode").should("eq", 200);
		cy.window().should((win) => {
			expect(win.frappe.document_management?.console_adapter).to.be.undefined;
		});

		cy.get(".statuses-dropdown-btn").click();
		cy.get('.statuses-dropdown-list input[value="Published"]').check();
		cy.wait("@documents").its("response.statusCode").should("eq", 200);
		openUpload();
		assertUploadControls();
		cy.get(".pc-modal-close").click();
		cy.get(".pc-modal-overlay").should("not.exist");
	});

	it("scrolls the upload fields while keeping its controls visible on a small screen", () => {
		cy.viewport(375, 568);
		cy.visit(consolePath);
		cy.wait("@documents").its("response.statusCode").should("eq", 200);
		openUpload();
		assertUploadControls();
		cy.get(".pc-modal-body").scrollTo("bottom").should(($body) => {
			expect($body[0].scrollTop, "upload fields scrolled").to.be.greaterThan(0);
		});
		assertUploadControls();
		cy.get(".pc-modal-footer .btn-default").click();
		cy.get(".pc-modal-overlay").should("not.exist");
	});
});
