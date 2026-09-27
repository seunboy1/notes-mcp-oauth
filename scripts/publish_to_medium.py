#!/usr/bin/env python
"""Publish the article to Medium with Playwright.

Medium retired its publishing API in 2023, so the only supported way to create
a post programmatically is to drive the editor in a real browser. That means
this script needs a logged-in Medium session, and it deliberately never asks
for your password.

How it works
------------
First run, you log in by hand once:

    uv run scripts/publish_to_medium.py --login

A visible browser opens medium.com. Sign in however you normally do (Google,
email link, whatever). When you land on your logged-in homepage, press Enter in
the terminal. The session cookies are saved to ``.medium-session.json`` and
reused from then on.

Then create the draft:

    uv run scripts/publish_to_medium.py                 # draft, opens for review
    uv run scripts/publish_to_medium.py --publish       # draft + publish
    uv run scripts/publish_to_medium.py --headless      # no visible window

The markdown is converted into Medium's editor as you type: headings become
real headings, code fences become code blocks, images are uploaded, and
horizontal rules become section dividers.

Notes
-----
* ``.medium-session.json`` holds live credentials. It is in .gitignore. Treat it
  like a password.
* The default is to stop at a draft. Nothing is published unless you pass
  --publish, and even then you get a confirmation prompt.
* Medium changes its DOM without notice. Selectors are defined once in
  SELECTORS below so a break is a one-line fix.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARTICLE = ROOT / "article" / "mcp-servers-explained-and-built.md"
SESSION_FILE = ROOT / ".medium-session.json"
PROFILE_DIR = ROOT / ".medium-profile"

NEW_STORY_URL = "https://medium.com/new-story"
HOME_URL = "https://medium.com/"

# One place to fix when Medium reshuffles its DOM.
SELECTORS = {
    "editor_title": [
        'h3[data-testid="editorTitleParagraph"]',
        'h3.graf--title',
        'div[contenteditable="true"] h3',
    ],
    "editor_body": [
        'div[data-testid="editorParagraphText"]',
        'div.section-inner p',
        'div[contenteditable="true"]',
    ],
    "publish_button": [
        'button[data-testid="headerPublishButton"]',
        'button:has-text("Publish")',
    ],
    "publish_now": [
        'button[data-testid="publishConfirmButton"]',
        'button:has-text("Publish now")',
    ],
    "signed_in_marker": [
        'button[aria-label="user options menu"]',
        'a[href="/new-story"]',
        'img[data-testid="authorPhoto"]',
    ],
}


# --------------------------------------------------------------------------- #
# Markdown -> a flat list of editor actions
# --------------------------------------------------------------------------- #
@dataclass
class Block:
    kind: str   # h1 | h2 | h3 | text | code | quote | rule | image | list
    text: str = ""
    path: Path | None = None
    continues: bool = False   # this list item follows another one


def parse_markdown(md: str, base_dir: Path) -> tuple[str, list[Block]]:
    """Split markdown into a title plus an ordered list of blocks.

    Intentionally small: Medium's editor supports a limited subset, so anything
    fancier than this would be discarded anyway.
    """
    lines = md.replace("\r\n", "\n").split("\n")
    title = ""
    blocks: list[Block] = []
    i = 0

    # The first H1 becomes the story title.
    while i < len(lines):
        if lines[i].startswith("# "):
            title = lines[i][2:].strip()
            i += 1
            break
        if lines[i].strip():
            break
        i += 1

    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            text = " ".join(s.strip() for s in paragraph).strip()
            if text:
                blocks.append(Block("text", clean_inline(text)))
            paragraph.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # fenced code
        if stripped.startswith("```"):
            flush()
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            if code:
                blocks.append(Block("code", "\n".join(code)))
            continue

        # images: ![alt](path)
        img = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", stripped)
        if img:
            flush()
            raw = img.group(2).strip()
            if not raw.startswith(("http://", "https://")):
                candidate = (base_dir / raw).resolve()
                if not candidate.exists():
                    candidate = (base_dir.parent / raw).resolve()
                if candidate.exists():
                    blocks.append(Block("image", img.group(1), candidate))
            i += 1
            continue

        # horizontal rule
        if re.fullmatch(r"(\*\s*){3,}|(-\s*){3,}|(_\s*){3,}", stripped):
            flush()
            blocks.append(Block("rule"))
            i += 1
            continue

        # headings
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            flush()
            level = len(heading.group(1))
            kind = "h1" if level <= 2 else ("h2" if level == 3 else "h3")
            blocks.append(Block(kind, clean_inline(heading.group(2).strip())))
            i += 1
            continue

        # blockquote
        if stripped.startswith("> "):
            flush()
            quote = [stripped[2:].strip()]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("> "):
                quote.append(lines[i].strip()[2:].strip())
                i += 1
            blocks.append(Block("quote", clean_inline(" ".join(quote))))
            continue

        # list item (Medium auto-formats when the line starts with "- " or "1. ")
        if re.match(r"^([-*+]|\d+\.)\s+", stripped):
            flush()
            blocks.append(Block("list", clean_inline(stripped)))
            i += 1
            continue

        # markdown table -> keep as monospace so it stays aligned
        if stripped.startswith("|") and stripped.endswith("|"):
            flush()
            table = [stripped]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                table.append(lines[i].strip())
                i += 1
            blocks.append(Block("code", "\n".join(table)))
            continue

        if not stripped:
            flush()
            i += 1
            continue

        paragraph.append(line)
        i += 1

    flush()

    # Mark consecutive list items so only the first types its bullet marker.
    for idx in range(1, len(blocks)):
        if blocks[idx].kind == "list" and blocks[idx - 1].kind == "list":
            blocks[idx].continues = True

    # After a list run ends, the editor is still in list mode; a trailing
    # Enter is emitted by type_block, so nothing extra is needed here.
    return title, blocks


def clean_inline(text: str) -> str:
    """Strip markdown emphasis that the editor cannot receive as literal text.

    Typing ``**bold**`` into Medium yields the asterisks verbatim, which looks
    worse than plain prose, so the markers are removed. Inline code keeps its
    backticks: Medium converts `like this` automatically.
    """
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\s)([^*]+?)(?<!\s)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<![\w_])_(?!\s)([^_]+?)(?<!\s)_(?![\w_])", r"\1", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# Playwright helpers
# --------------------------------------------------------------------------- #
async def first_visible(page, keys: list[str], timeout: float = 8000):
    """Return the first selector in ``keys`` that resolves, or None."""
    for selector in keys:
        try:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=timeout)
            return locator
        except Exception:
            continue
    return None


async def is_signed_in(page) -> bool:
    for selector in SELECTORS["signed_in_marker"]:
        if await page.locator(selector).count():
            return True
    return False


async def open_browser(pw, headless: bool):
    """Launch a persistent-profile browser.

    A persistent profile matters for two reasons: the Medium login survives
    between runs, and Cloudflare is far more willing to serve a browser that
    looks like a returning user. Headless Chromium gets a 403 from Medium's
    bot protection, so headless is opt-in and warned about.
    """
    PROFILE_DIR.mkdir(exist_ok=True)
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    if SESSION_FILE.exists():
        try:
            cookies = json.loads(SESSION_FILE.read_text()).get("cookies", [])
            if cookies:
                await context.add_cookies(cookies)
        except Exception:
            pass
    return context


async def do_login(headless: bool, wait_seconds: int = 420) -> int:
    """Interactive one-time login.

    Opens a real browser window and then simply watches for a signed-in
    session, so it works whether or not this process has an attached terminal.
    Nothing is typed for you and no password is ever requested.
    """
    from playwright.async_api import async_playwright

    if headless:
        print("Login needs a visible browser. Drop --headless for this step.")
        return 2

    async with async_playwright() as pw:
        context = await open_browser(pw, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(f"{HOME_URL}m/signin", wait_until="domcontentloaded")

        print("\n" + "=" * 68)
        print("  A browser window is open on Medium's sign-in page.")
        print("  Sign in however you normally do (Google, email link, ...).")
        print(f"  I will watch for up to {wait_seconds // 60} minutes and save the")
        print("  session automatically once you are in. No password is read here.")
        print("=" * 68 + "\n", flush=True)

        deadline = asyncio.get_event_loop().time() + wait_seconds
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3)
            try:
                if await is_signed_in(page):
                    await page.wait_for_timeout(2500)
                    await context.storage_state(path=str(SESSION_FILE))
                    SESSION_FILE.chmod(0o600)
                    print(f"Signed in. Session saved to {SESSION_FILE.name} "
                          f"and {PROFILE_DIR.name}/.", flush=True)
                    print("Both are credentials and are in .gitignore.", flush=True)
                    await context.close()
                    return 0
            except Exception:
                continue   # mid-navigation; try again

        print("Timed out without detecting a signed-in session.", flush=True)
        await context.close()
    return 1


MOD = "Meta" if sys.platform == "darwin" else "Control"

# Verified empirically against the live editor (see article/README notes):
#   Meta/Ctrl+Alt+1/2/3  headings
#   Meta/Ctrl+Alt+5      blockquote
#   Meta/Ctrl+Alt+6      code block   <- NOT Alt+K, which does nothing
SHORTCUTS = {
    "h1": f"{MOD}+Alt+1",
    "h2": f"{MOD}+Alt+2",
    "h3": f"{MOD}+Alt+3",
    "quote": f"{MOD}+Alt+5",
    "code": f"{MOD}+Alt+6",
}


async def escape_code_block(page) -> None:
    """Leave a code block without collapsing it.

    Toggling the shortcut off turns the PRE back into a paragraph, and pressing
    Enter twice leaves the caret inside. The reliable move is to press Enter
    once (which creates a trailing paragraph) and then click that paragraph.
    """
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(250)
    trailing = page.locator("div.section-inner > p").last
    try:
        await trailing.click(timeout=3000)
    except Exception:
        pass
    await page.wait_for_timeout(150)


async def ensure_empty_paragraph(page) -> None:
    """Make sure the caret sits on an empty paragraph.

    The "+" toolbar only appears on an empty block, so a divider or image that
    follows a paragraph of prose needs a blank line created first. Without this
    the toolbar is invisible and the insert silently does nothing.
    """
    for _ in range(3):
        empty = await page.evaluate(
            """() => {
                 const r = document.querySelector('div.section-inner');
                 if (!r || !r.lastElementChild) return false;
                 const el = r.lastElementChild;
                 return el.tagName === 'P' && !(el.innerText || '').trim();
               }"""
        )
        if empty:
            return
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(250)


async def open_inline_menu(page) -> bool:
    """Expand the editor's "+" menu on the current empty paragraph.

    The menu's buttons exist in the DOM at all times but are invisible until
    this toggle is clicked, so clicking them directly always times out.
    """
    await ensure_empty_paragraph(page)
    toggle = page.locator('button[data-action="inline-menu"]').first
    for _ in range(2):
        try:
            if await toggle.is_visible():
                await toggle.click()
                await page.wait_for_timeout(500)
                return True
        except Exception:
            pass
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(350)
    return False


async def insert_divider(page) -> bool:
    """Insert a section divider via the toolbar.

    Typing "---" does not autoconvert in the current editor - it is left as
    literal text - so the toolbar button is the only reliable route.
    """
    if not await open_inline_menu(page):
        return False
    try:
        btn = page.locator('button[data-action="inline-menu-hr"]').first
        await btn.click(timeout=5000)
        await page.wait_for_timeout(900)
        return True
    except Exception:
        return False


async def insert_image(page, block: Block) -> bool:
    """Upload an image through the toolbar's file chooser."""
    assert block.path is not None
    if not await open_inline_menu(page):
        return False
    try:
        async with page.expect_file_chooser(timeout=8000) as fc:
            await page.locator('button[data-action="inline-menu-image"]').first.click()
        chooser = await fc.value
        await chooser.set_files(str(block.path))
        await page.wait_for_timeout(6000)   # wait for the upload to settle
        return True
    except Exception:
        return False


async def type_block(page, editor, block: Block) -> None:
    """Enter one block into the Medium editor using its keyboard shortcuts."""
    kb = page.keyboard

    if block.kind == "rule":
        if not await insert_divider(page):
            await kb.press("Enter")   # fall back to plain whitespace
        return

    if block.kind == "image":
        if not await insert_image(page, block):
            print(f"    ! could not attach image {block.path.name} - "
                  f"add it by hand in the draft")
            return
        if block.text:
            await kb.type(block.text, delay=1)   # caption
        await kb.press("Enter")
        return

    if block.kind == "code":
        await kb.press(SHORTCUTS["code"])
        await page.wait_for_timeout(200)
        lines = block.text.split("\n")
        for idx, line in enumerate(lines):
            if line:
                await kb.type(line, delay=1)
            if idx < len(lines) - 1:
                await kb.press("Shift+Enter")
        await escape_code_block(page)
        return

    if block.kind == "list":
        # Medium continues a list automatically on Enter, so only the first
        # item in a run carries its marker; later ones would print "- " twice.
        text = block.text
        if not block.continues:
            await kb.type(text[:2], delay=30)    # "- " or "1." triggers the list
            await page.wait_for_timeout(350)
            text = text[2:].lstrip()
        else:
            text = re.sub(r"^([-*+]|\d+\.)\s+", "", text)
        await kb.type(text, delay=1)
        await kb.press("Enter")
        return

    shortcut = SHORTCUTS.get(block.kind)
    if shortcut:
        await kb.press(shortcut)
        await page.wait_for_timeout(120)

    await kb.type(block.text, delay=1)
    await kb.press("Enter")

    # Headings and quotes persist to the next paragraph, so reset to body text.
    if block.kind in ("h1", "h2", "h3", "quote"):
        await kb.press(f"{MOD}+Alt+0")
        await page.wait_for_timeout(120)


async def publish(args: argparse.Namespace) -> int:
    from playwright.async_api import async_playwright

    article = Path(args.article).resolve()
    if not article.exists():
        print(f"Article not found: {article}")
        return 2
    # --dry-run only parses, so it must not require a session.
    if not args.dry_run and not (SESSION_FILE.exists() or PROFILE_DIR.exists()):
        print("No saved Medium session.\n  Run:  uv run scripts/publish_to_medium.py --login")
        return 2

    title, blocks = parse_markdown(article.read_text(), article.parent)
    if args.title:
        title = args.title
    if not title:
        print("Could not determine a title (no H1 found). Pass --title.")
        return 2

    counts: dict[str, int] = {}
    for b in blocks:
        counts[b.kind] = counts.get(b.kind, 0) + 1
    print(f"\nArticle : {article.name}")
    print(f"Title   : {title}")
    print(f"Blocks  : {len(blocks)}  " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if args.dry_run:
        print("\n--dry-run: parsed only, browser not launched.")
        for b in blocks[:12]:
            preview = (b.text or (b.path.name if b.path else "")).replace("\n", " / ")[:78]
            print(f"  {b.kind:6} {preview}")
        if len(blocks) > 12:
            print(f"  ... and {len(blocks) - 12} more")
        return 0

    async with async_playwright() as pw:
        if args.headless:
            print("\nNote: Medium's bot protection returns 403 to headless Chromium.")
            print("      If this fails, rerun without --headless.")
        context = await open_browser(pw, headless=args.headless)
        page = context.pages[0] if context.pages else await context.new_page()

        print("\nOpening the Medium editor...")
        await page.goto(NEW_STORY_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        if "signin" in page.url or "m/signin" in page.url:
            print("Medium redirected to sign-in: the saved session expired.")
            print("  Run:  uv run scripts/publish_to_medium.py --login")
            await context.close()
            return 1

        title_el = await first_visible(page, SELECTORS["editor_title"], timeout=15000)
        if title_el is None:
            shot = ROOT / "article" / "medium-editor-debug.png"
            await page.screenshot(path=str(shot), full_page=True)
            print(f"Could not find the editor title field. Screenshot: {shot}")
            await context.close()
            return 1

        await title_el.click()
        await page.keyboard.type(title, delay=2)
        await page.keyboard.press("Enter")
        print(f"  title set: {title}")

        print(f"  writing {len(blocks)} blocks...")
        for idx, block in enumerate(blocks, 1):
            try:
                await type_block(page, title_el, block)
            except Exception as exc:  # keep going; one bad block is not fatal
                print(f"    ! block {idx} ({block.kind}) failed: {exc}")
            if idx % 20 == 0:
                print(f"    {idx}/{len(blocks)}")
                await page.wait_for_timeout(400)

        await page.wait_for_timeout(3000)  # let autosave settle
        draft_url = page.url
        print(f"\nDraft saved: {draft_url}")

        if args.publish:
            print("\nAbout to PUBLISH this story publicly.")
            answer = await asyncio.get_event_loop().run_in_executor(
                None, lambda: input("Type 'publish' to confirm: ").strip().lower()
            )
            if answer == "publish":
                btn = await first_visible(page, SELECTORS["publish_button"], timeout=10000)
                if btn is None:
                    print("Could not find the Publish button; the draft is saved.")
                else:
                    await btn.click()
                    await page.wait_for_timeout(2500)
                    now = await first_visible(page, SELECTORS["publish_now"], timeout=10000)
                    if now is None:
                        print("Could not find the final confirm button; the draft is saved.")
                    else:
                        await now.click()
                        await page.wait_for_timeout(6000)
                        print(f"Published: {page.url}")
            else:
                print("Not confirmed - left as a draft.")
        else:
            print("Left as a draft. Add --publish to go live.")

        if not args.headless:
            print("\nBrowser stays open so you can review. Press Enter to close...")
            await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)

        (ROOT / "article" / "last-draft.json").write_text(
            json.dumps({"title": title, "url": draft_url, "blocks": len(blocks)}, indent=2)
        )
        await context.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Publish the article to Medium via Playwright.")
    ap.add_argument("--article", default=str(DEFAULT_ARTICLE), help="markdown file to publish")
    ap.add_argument("--title", help="override the title (default: the first H1)")
    ap.add_argument("--login", action="store_true", help="interactive one-time login")
    ap.add_argument("--publish", action="store_true", help="publish, not just draft")
    ap.add_argument("--headless", action="store_true", help="run without a visible window")
    ap.add_argument("--dry-run", action="store_true", help="parse only; do not open a browser")
    args = ap.parse_args()

    if args.login:
        return asyncio.run(do_login(args.headless))
    return asyncio.run(publish(args))


if __name__ == "__main__":
    raise SystemExit(main())
