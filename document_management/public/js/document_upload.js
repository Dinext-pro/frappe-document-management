frappe.provide('frappe.document_management');

frappe.document_management.upload_document_file = async function(file, method, fields) {
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
        let message = payload.exception || __('File upload failed.');
        if (payload._server_messages) {
            try {
                const parsed = JSON.parse(payload._server_messages);
                if (Array.isArray(parsed) && parsed.length) {
                    const first = JSON.parse(parsed[0]);
                    if (first && first.message) {
                        message = first.message;
                    }
                }
            } catch {
                // Fall back to payload.exception
            }
        }
        throw new Error(message);
    }
    return payload.message;
};
