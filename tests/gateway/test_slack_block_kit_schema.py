import json
import re

import pytest

from plugins.platforms.slack.block_kit import MAX_SECTION_TEXT, render_blocks


@pytest.mark.parametrize("markdown", ["# ***", "> ", "```\n\n```"])
def test_empty_structural_content_declines_instead_of_emitting_empty_text(markdown):
    # Given markdown that classifies as a structural block but has no content.
    # When it is rendered to Slack Block Kit.
    blocks = render_blocks(markdown)

    # Then the renderer declines instead of emitting an invalid empty text node.
    assert blocks is None


def test_blank_table_cells_have_nonempty_rich_text_payloads():
    # Given a valid native table containing blank cells.
    markdown = "| | Value |\n|---|---|\n| | ok |"

    # When the table is rendered.
    blocks = render_blocks(markdown)

    # Then every rich-text table cell contains only nonempty text elements.
    assert blocks is not None
    table = blocks[0]
    assert table["type"] == "table"
    for row in table["rows"]:
        for cell in row:
            for section in cell["elements"]:
                assert section["elements"]
                for element in section["elements"]:
                    if element["type"] == "text":
                        assert element["text"]


def test_blank_quote_line_never_emits_an_empty_rich_text_child():
    # Given a nonempty quote containing a blank quoted line.
    # When the quote is rendered.
    blocks = render_blocks("> alpha\n> \n> omega")

    # Then the visual blank line remains without an empty text element.
    assert blocks is not None
    children = blocks[0]["elements"][0]["elements"]
    assert all(element.get("text") for element in children if element["type"] == "text")


def test_second_native_table_degrades_without_losing_its_content():
    # Given two otherwise-valid markdown tables in one message.
    markdown = (
        "| First | Value |\n|---|---|\n| alpha | 1 |\n\n"
        "| Second | Value |\n|---|---|\n| beta | 2 |"
    )

    # When the message is rendered.
    blocks = render_blocks(markdown)

    # Then only one native table ships and the other table safely degrades.
    assert blocks is not None
    assert [block["type"] for block in blocks].count("table") == 1
    preformatted = [
        block
        for block in blocks
        if block["type"] == "rich_text"
        and block["elements"][0]["type"] == "rich_text_preformatted"
    ]
    assert len(preformatted) == 1
    assert "Second" in json.dumps(preformatted)
    assert "beta" in json.dumps(preformatted)


def test_relative_paragraph_link_never_reaches_formatter_as_a_slack_link():
    # Given a formatter that would turn a relative Markdown link into malformed mrkdwn.
    def format_like_slack_adapter(text):
        return text.replace("[guide](/guide)", "</guide|guide>")

    # When a paragraph containing that relative link is rendered.
    blocks = render_blocks(
        "See [guide](/guide) for details.",
        mrkdwn_fn=format_like_slack_adapter,
    )

    # Then the destination is retained as plain text, never a malformed Slack link token.
    assert blocks is not None
    payload = json.dumps(blocks)
    assert "</guide|guide>" not in payload
    assert "/guide" in payload


def test_angle_wrapped_absolute_link_still_reaches_formatter_as_markdown():
    # Given a valid absolute Markdown link with optional angle delimiters.
    def format_like_slack_adapter(text):
        return text.replace(
            "[docs](<https://example.com/docs>)",
            "<https://example.com/docs|docs>",
        )

    # When the paragraph is rendered through the Slack-style formatter.
    blocks = render_blocks(
        "Read [docs](<https://example.com/docs>)",
        mrkdwn_fn=format_like_slack_adapter,
    )

    # Then the valid absolute Slack token survives.
    assert blocks is not None
    assert "<https://example.com/docs|docs>" in json.dumps(blocks)


def test_relative_list_link_degrades_to_text_instead_of_a_link_element():
    # Given a relative Markdown link inside a rich-text list.
    # When the list is rendered.
    blocks = render_blocks("- Read [guide](/guide)")

    # Then no Slack link element carries a relative URL and the destination survives.
    assert blocks is not None
    children = blocks[0]["elements"][0]["elements"][0]["elements"]
    assert not [element for element in children if element["type"] == "link"]
    assert "/guide" in json.dumps(children)


def test_relative_markdown_syntax_inside_inline_code_stays_opaque():
    # Given relative-link syntax inside an inline-code span.
    # When the span is rendered in rich text.
    blocks = render_blocks("- `[guide](/guide)`")

    # Then link safety does not rewrite the code contents.
    assert blocks is not None
    children = blocks[0]["elements"][0]["elements"][0]["elements"]
    assert children == [
        {"type": "text", "text": "[guide](/guide)", "style": {"code": True}}
    ]


def test_section_chunking_keeps_slack_link_token_whole():
    # Given formatted mrkdwn whose section boundary falls inside a Slack link token.
    token = "<https://example.com/reference|reference documentation>"
    rendered = "x" * (MAX_SECTION_TEXT - 10) + token + " tail"

    # When the oversized paragraph is rendered into multiple sections.
    blocks = render_blocks("source", mrkdwn_fn=lambda _text: rendered)

    # Then the token is wholly contained in one section and content is preserved.
    assert blocks is not None
    chunks = [block["text"]["text"] for block in blocks]
    assert all(len(chunk) <= MAX_SECTION_TEXT for chunk in chunks)
    assert any(token in chunk for chunk in chunks)
    assert "".join(chunks) == rendered


def test_oversized_slack_link_token_degrades_before_hard_chunking():
    # Given one Slack link token that cannot fit in a section by itself.
    token = f"<https://example.com/{'x' * MAX_SECTION_TEXT}|label>"

    # When the paragraph is rendered.
    blocks = render_blocks("source", mrkdwn_fn=lambda _text: token)

    # Then every chunk is schema-sized and no chunk contains a partial Slack token.
    assert blocks is not None
    chunks = [block["text"]["text"] for block in blocks]
    assert all(len(chunk) <= MAX_SECTION_TEXT for chunk in chunks)
    assert all(re.search(r"<[^>]*$|^[^<]*>", chunk) is None for chunk in chunks)
    assert "label" in "".join(chunks)
