from markdown_it import MarkdownIt


_parser = MarkdownIt("commonmark", {"html": True, "tasklists": True,
                                    "strikethrough_single_tilde": True}).enable(["table", "strikethrough"])


def markdown_visible_text(source: str) -> str:
    """Extract normalized selectable text, matching GFM rendering with HTML skipped."""
    parts = []

    def collect(tokens):
        for token in tokens:
            if token.type in {"text", "code_inline", "code_block", "fence"}:
                parts.append(token.content)
            elif token.type in {"softbreak", "hardbreak"}:
                parts.append("\n")
            elif token.children and token.type != "image":
                collect(token.children)
            # Inline HTML tags disappear while their ordinary text children remain.
            # An HTML block is one opaque token and contributes no selectable text.
            if token.block and token.nesting != 1:
                parts.append("\n")

    collect(_parser.parse(source))
    return " ".join("".join(parts).split())
