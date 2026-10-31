"""
Upload validation by content signature. Filenames and browser-supplied
content types are not trusted.
"""
import os
import zipfile

from django.conf import settings

from apps.core.exceptions import BusinessRuleViolation

_OLE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'

# extension → (detected mime, signature check)
_SIGNATURES = {
    '.pdf': ('application/pdf', lambda h: h.startswith(b'%PDF-')),
    '.png': ('image/png', lambda h: h.startswith(b'\x89PNG\r\n\x1a\n')),
    '.jpg': ('image/jpeg', lambda h: h.startswith(b'\xff\xd8\xff')),
    '.jpeg': ('image/jpeg', lambda h: h.startswith(b'\xff\xd8\xff')),
    '.tiff': ('image/tiff', lambda h: h[:4] in (b'II*\x00', b'MM\x00*')),
    '.bmp': ('image/bmp', lambda h: h.startswith(b'BM')),
    '.xls': ('application/vnd.ms-excel', lambda h: h.startswith(_OLE)),
    '.doc': ('application/msword', lambda h: h.startswith(_OLE)),
    '.xlsx': ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', None),
    '.docx': ('application/vnd.openxmlformats-officedocument.wordprocessingml.document', None),
    '.csv': ('text/csv', None),
    '.txt': ('text/plain', None),
}
_OOXML_MARKER = {'.xlsx': 'xl/', '.docx': 'word/'}


def _read_head(f, n=8192):
    f.seek(0)
    head = f.read(n)
    f.seek(0)
    return head


def _is_text(f):
    head = _read_head(f)
    if b'\x00' in head:
        return False
    try:
        head.decode('utf-8')
    except UnicodeDecodeError as exc:
        # Allow a multi-byte character cut at the read boundary
        if exc.start < len(head) - 4:
            return False
    return True


def _is_ooxml(f, ext):
    try:
        f.seek(0)
        with zipfile.ZipFile(f) as zf:
            names = zf.namelist()
        return '[Content_Types].xml' in names and any(n.startswith(_OOXML_MARKER[ext]) for n in names)
    except zipfile.BadZipFile:
        return False
    finally:
        f.seek(0)


def validate_upload(uploaded_file):
    """Validate size, extension and content signature. Returns the detected MIME type."""
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if uploaded_file.size > max_bytes:
        raise BusinessRuleViolation(
            f'File exceeds the {settings.MAX_UPLOAD_SIZE_MB} MB limit.', details={'file': ['Too large.']},
        )
    if uploaded_file.size == 0:
        raise BusinessRuleViolation('File is empty.', details={'file': ['Empty file.']})
    ext = os.path.splitext(uploaded_file.name)[1].lower()
    if ext not in settings.ALLOWED_UPLOAD_EXTENSIONS or ext not in _SIGNATURES:
        raise BusinessRuleViolation(f'File type "{ext or "none"}" is not allowed.', details={'file': ['Type not allowed.']})

    mime, check = _SIGNATURES[ext]
    if ext in _OOXML_MARKER:
        ok = _is_ooxml(uploaded_file, ext)
    elif ext in ('.csv', '.txt'):
        ok = _is_text(uploaded_file)
    else:
        ok = check(_read_head(uploaded_file, 16))
    if not ok:
        raise BusinessRuleViolation(
            f'File content does not match the "{ext}" extension.', details={'file': ['Content mismatch.']},
        )
    return mime
