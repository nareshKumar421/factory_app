"""Best-effort helpers used when allocating codes for uploaded files."""

import re


def count_pdf_pages(django_file) -> int:
    """Return the page count of an uploaded PDF, best-effort (``1`` on failure).

    Tries ``pypdf`` / ``PyPDF2`` when available; otherwise falls back to a
    lightweight scan of the raw bytes. Never raises -- page count is a nicety,
    not a gate, so a weird file must not block an upload.
    """
    try:
        pos = django_file.tell()
    except (AttributeError, ValueError, OSError):
        pos = None
    try:
        try:
            django_file.seek(0)
        except (AttributeError, ValueError, OSError):
            pass

        # Preferred: a real PDF parser if the environment has one.
        for module_name in ("pypdf", "PyPDF2"):
            try:
                reader_mod = __import__(module_name)
            except ImportError:
                continue
            try:
                reader = reader_mod.PdfReader(django_file)
                pages = len(reader.pages)
                if pages > 0:
                    return pages
            except Exception:
                # Fall through to the byte-scan fallback.
                pass
            finally:
                try:
                    django_file.seek(0)
                except (AttributeError, ValueError, OSError):
                    pass

        # Fallback: count "/Type /Page" objects in the raw bytes.
        try:
            data = django_file.read()
            if isinstance(data, str):
                data = data.encode("latin-1", "ignore")
            matches = re.findall(rb"/Type\s*/Page[^s]", data)
            if matches:
                return len(matches)
        except Exception:
            pass
        return 1
    finally:
        if pos is not None:
            try:
                django_file.seek(pos)
            except (AttributeError, ValueError, OSError):
                pass
