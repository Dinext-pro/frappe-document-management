import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const helpers = require('./console_helpers.js');

test('markdown_anchor_slug normalizes unicode headings', () => {
    assert.equal(helpers.markdown_anchor_slug('Sección 1: Resumen General!'), 'sección-1-resumen-general');
    assert.equal(helpers.markdown_anchor_slug('  Hello   World  '), 'hello-world');
});

test('escape_html escapes all HTML special characters', () => {
    assert.equal(
        helpers.escape_html(`<script>alert("x" & 'y')</script>`),
        '&lt;script&gt;alert(&quot;x&quot; &amp; &#039;y&#039;)&lt;/script&gt;'
    );
});

test('highlight_text highlights terms safely without XSS', () => {
    const output = helpers.highlight_text('Contract <img src=x> agreement', ['contract', 'agreement']);
    assert.equal(output, '<mark>Contract</mark> &lt;img src=x&gt; <mark>agreement</mark>');
});

test('get_contrast_color and get_tag_style handle valid and invalid colors', () => {
    assert.equal(helpers.get_contrast_color('#000000'), '#ffffff');
    assert.equal(helpers.get_contrast_color('#ffffff'), '#000000');
    assert.deepEqual(helpers.get_tag_style('invalid-css;color:red'), {
        '--tag-color': '#e2e8f0',
        '--tag-contrast': '#000000',
    });
    assert.deepEqual(helpers.get_tag_style('#1e293b'), {
        '--tag-color': '#1e293b',
        '--tag-contrast': '#ffffff',
    });
});

test('file type detection excludes svg from inline image preview', () => {
    assert.equal(helpers.is_img('/private/files/photo.png'), true);
    assert.equal(helpers.is_img('/private/files/vector.svg'), false);
    assert.equal(helpers.get_file_type({ original_file: '/private/files/vector.svg' }), 'generic');
    assert.equal(helpers.get_icon({ original_file: '/private/files/report.pdf' }), 'PDF');
    assert.equal(helpers.get_icon({ original_file: '/private/files/notes.md' }), 'MD');
});

test('console links retain the active Desk root and encode the document and page', () => {
    const params = { Doc: 'A & B/2', page: 3 };
    assert.equal(helpers.desk_page_url('document-management-console', params, '/app/workspace'),
        '/app/document-management-console?Doc=A+%26+B%2F2&page=3');
    assert.equal(helpers.desk_page_url('document-management-console', {}, '/desk/document-management-console'),
        '/desk/document-management-console');
});
