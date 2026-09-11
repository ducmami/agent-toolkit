"""Transform HTML fragments into Confluence storage-format XHTML."""

from __future__ import annotations

import html as html_lib
import re
from bs4 import BeautifulSoup, NavigableString, Tag

from .helpers import map_language, warn

# Confluence storage macros use these prefixes without requiring xmlns on the fragment.
AC = "ac"
RI = "ri"

TOC_MACRO = (
    f'<{AC}:structured-macro {AC}:name="toc" {AC}:schema-version="1">'
    f"</{AC}:structured-macro>"
)

LANGUAGE_CLASS_RE = re.compile(r"(?:language|lang)-([^\s]+)", re.I)
MERMAID_WRAPPER_CLASS = "doconvert-mermaid"
MERMAID_EXPAND_TITLE = "Mermaid source"

# GFM / pymdown admonition type → Confluence panel macro name (severity mapping).
ADMONITION_TO_MACRO = {
    "note": "info",
    "tip": "tip",
    "important": "note",
    "warning": "warning",
    "caution": "warning",
}


def _new_tag(soup: BeautifulSoup, name: str, attrs: dict[str, str] | None = None) -> Tag:
    tag = soup.new_tag(name)
    if attrs:
        for key, value in attrs.items():
            tag[key] = value
    return tag


def _cdata(text: str) -> str:
    # Confluence plain-text-body expects CDATA; escape nested ]]> if present.
    safe = text.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{safe}]]>"


def _extract_language(code: Tag) -> str | None:
    classes = code.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    for cls in classes:
        match = LANGUAGE_CLASS_RE.match(cls)
        if match:
            return map_language(match.group(1))
    return map_language(None)


def _make_code_macro(soup: BeautifulSoup, code: Tag) -> Tag:
    language = _extract_language(code)
    source = code.get_text()
    macro = _new_tag(
        soup,
        f"{AC}:structured-macro",
        {f"{AC}:name": "code", f"{AC}:schema-version": "1"},
    )
    if language:
        param = _new_tag(soup, f"{AC}:parameter", {f"{AC}:name": "language"})
        param.string = language
        macro.append(param)
    body = _new_tag(soup, f"{AC}:plain-text-body")
    # BeautifulSoup may escape CDATA if set as string; use NavigableString carefully.
    # Insert raw CDATA via a placeholder then replace in serializer.
    body.append(NavigableString(_cdata(source)))
    macro.append(body)
    return macro


def _make_image(soup: BeautifulSoup, img: Tag, image_base_url: str | None) -> Tag:
    src = (img.get("src") or "").strip()
    alt = (img.get("alt") or "").strip()
    if not src:
        warn("Skipping <img> with empty src")
        return _new_tag(soup, "p")

    if not (src.startswith("http://") or src.startswith("https://") or src.startswith("data:")):
        if image_base_url:
            base = image_base_url.rstrip("/")
            src = f"{base}/{src.lstrip('/')}"
        else:
            warn(f"Relative image src without --image-base-url: {src}")

    attrs: dict[str, str] = {}
    if alt:
        attrs[f"{AC}:alt"] = alt
    image = _new_tag(soup, f"{AC}:image", attrs)
    url = _new_tag(soup, f"{RI}:url", {f"{RI}:value": src})
    image.append(url)
    return image


def _is_task_list(ul: Tag) -> bool:
    for li in ul.find_all("li", recursive=False):
        classes = li.get("class") or []
        if isinstance(classes, str):
            classes = classes.split()
        if "task-list-item" in classes:
            return True
        if li.find("input", attrs={"type": "checkbox"}):
            return True
    return False


def _make_task_list(soup: BeautifulSoup, ul: Tag) -> Tag:
    task_list = _new_tag(soup, f"{AC}:task-list")
    for li in ul.find_all("li", recursive=False):
        checkbox = li.find("input", attrs={"type": "checkbox"})
        complete = False
        if checkbox is not None:
            complete = checkbox.has_attr("checked")
            checkbox.decompose()
        # pymdownx custom_checkbox wraps indicator in <label class="task-list-control">
        for label in list(li.find_all("label", class_="task-list-control")):
            label.decompose()
        for span in list(li.find_all("span", class_="task-list-indicator")):
            span.decompose()

        task = _new_tag(soup, f"{AC}:task")
        status = _new_tag(soup, f"{AC}:task-status")
        status.string = "complete" if complete else "incomplete"
        body = _new_tag(soup, f"{AC}:task-body")
        text = li.get_text(strip=True)
        if text:
            body.string = text
        else:
            for child in list(li.contents):
                body.append(child.extract() if hasattr(child, "extract") else child)
        task.append(status)
        task.append(body)
        task_list.append(task)
    return task_list


def _admonition_type(div: Tag) -> str | None:
    """Return the admonition type class (e.g. note), excluding the base 'admonition' class."""
    classes = div.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    for cls in classes:
        if cls and cls != "admonition":
            return cls.lower()
    return None


def _make_expand_macro(soup: BeautifulSoup, title: str, inner: Tag) -> Tag:
    """Wrap ``inner`` in a Confluence expand macro (collapsed by default)."""
    macro = _new_tag(
        soup,
        f"{AC}:structured-macro",
        {f"{AC}:name": "expand", f"{AC}:schema-version": "1"},
    )
    if title:
        param = _new_tag(soup, f"{AC}:parameter", {f"{AC}:name": "title"})
        param.string = title
        macro.append(param)
    rich = _new_tag(soup, f"{AC}:rich-text-body")
    rich.append(inner)
    macro.append(rich)
    return macro


def _extract_mermaid_image(div: Tag) -> Tag | None:
    image = div.find(f"{AC}:image")
    if image is not None:
        return image.extract()
    img = div.find("img")
    if img is not None:
        return img.extract()
    return None


def _extract_mermaid_source(div: Tag) -> Tag | None:
    for tag in div.find_all(f"{AC}:structured-macro"):
        if tag.get(f"{AC}:name") == "code":
            return tag.extract()
    pre = div.find("pre")
    if pre is not None:
        return pre.extract()
    return None


def _replace_mermaid_wrapper(soup: BeautifulSoup, div: Tag) -> None:
    """Replace ``div.doconvert-mermaid`` with visible image + expand(source)."""
    image = _extract_mermaid_image(div)
    source = _extract_mermaid_source(div)
    replacements: list[Tag] = []
    if image is not None:
        replacements.append(image)
    if source is not None:
        replacements.append(_make_expand_macro(soup, MERMAID_EXPAND_TITLE, source))
    if replacements:
        div.replace_with(*replacements)
    else:
        div.decompose()


def _make_admonition_macro(soup: BeautifulSoup, div: Tag) -> Tag | None:
    """Convert ``div.admonition.<type>`` to a Confluence info/tip/note/warning macro."""
    admon_type = _admonition_type(div)
    if not admon_type:
        return None
    macro_name = ADMONITION_TO_MACRO.get(admon_type)
    if not macro_name:
        return None

    title_el = div.find("p", class_="admonition-title")
    title = title_el.get_text(strip=True) if title_el is not None else admon_type.title()
    if title_el is not None:
        title_el.decompose()

    macro = _new_tag(
        soup,
        f"{AC}:structured-macro",
        {f"{AC}:name": macro_name, f"{AC}:schema-version": "1"},
    )
    if title:
        param = _new_tag(soup, f"{AC}:parameter", {f"{AC}:name": "title"})
        param.string = title
        macro.append(param)

    rich = _new_tag(soup, f"{AC}:rich-text-body")
    for child in list(div.contents):
        rich.append(child.extract() if hasattr(child, "extract") else child)
    macro.append(rich)
    return macro


def _self_close_voids(html: str) -> str:
    """Ensure void elements are self-closed for XHTML storage."""
    voids = ("br", "hr", "img", "col", "wbr")
    for name in voids:
        html = re.sub(
            rf"<{name}(\s[^/>]*?)?(?<!/)\s*>",
            rf"<{name}\1 />",
            html,
            flags=re.I,
        )
    return html


def _unescape_cdata_match(match: re.Match[str]) -> str:
    return f"<![CDATA[{html_lib.unescape(match.group(1))}]]>"


def _serialize_fragment(soup: BeautifulSoup) -> str:
    """Serialize body children; preserve CDATA sections."""
    parts: list[str] = []
    root = soup.body if soup.body else soup
    for child in root.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        else:
            parts.append(str(child))
    html = "".join(parts)
    # BeautifulSoup escapes <![CDATA[...]]> to &lt;![CDATA[...]]&gt;
    # Restore CDATA markers that we intentionally inserted.
    html = html.replace("&lt;![CDATA[", "<![CDATA[").replace("]]&gt;", "]]>")
    # Also unescape entities BS introduced inside CDATA (e.g. mermaid ``-->``).
    html = re.sub(r"<!\[CDATA\[(.*?)\]\]>", _unescape_cdata_match, html, flags=re.DOTALL)
    html = _self_close_voids(html)
    return html.strip()


def html_to_storage(
    html: str,
    *,
    image_base_url: str | None = None,
    toc: bool = False,
    no_code_macro: bool = False,
) -> str:
    """Convert an HTML fragment to Confluence storage XHTML."""
    soup = BeautifulSoup(f"<body>{html}</body>", "lxml")

    for tag in soup.find_all(["script", "style"]):
        tag.decompose()

    # Code fences → Confluence code macro
    if not no_code_macro:
        for pre in list(soup.find_all("pre")):
            code = pre.find("code")
            if code is None:
                continue
            macro = _make_code_macro(soup, code)
            pre.replace_with(macro)

    # Images → ac:image
    for img in list(soup.find_all("img")):
        img.replace_with(_make_image(soup, img, image_base_url))

    # Mermaid wrapper → visible image + expand(source)
    for div in list(soup.find_all("div", class_=MERMAID_WRAPPER_CLASS)):
        _replace_mermaid_wrapper(soup, div)

    # Task lists
    for ul in list(soup.find_all("ul")):
        if _is_task_list(ul):
            ul.replace_with(_make_task_list(soup, ul))

    # GFM / pymdown admonitions → Confluence info/tip/note/warning macros
    # Process innermost first so nested callouts convert correctly.
    for div in list(reversed(soup.find_all("div", class_="admonition"))):
        macro = _make_admonition_macro(soup, div)
        if macro is not None:
            div.replace_with(macro)

    body = _serialize_fragment(soup)
    if toc:
        body = f"{TOC_MACRO}\n{body}" if body else TOC_MACRO
    return body
