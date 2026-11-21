const consolePath = "/app/document-management-console";
const method = (name) => "**/api/method/**document_management_console." + name;

describe("Standalone document console", () => {
	it("lists documents and exposes the native filters and actions without a provider", () => {
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
		cy.visit(consolePath);
		cy.wait("@documents").its("response.statusCode").should("eq", 200);
		cy.window().should((win) => {
			expect(win.frappe.document_management?.console_adapter).to.be.undefined;
		});

		cy.get(".statuses-dropdown-btn").click();
		cy.get('.statuses-dropdown-list input[value="Published"]').check();
		cy.wait("@documents").its("response.statusCode").should("eq", 200);
		cy.get(".paperless-toolbar .btn-upload").click();
		cy.get(".pc-modal-overlay .pc-modal").should("be.visible");
		cy.get(".pc-modal-close").click();
		cy.get(".pc-modal-overlay").should("not.exist");
	});
});
